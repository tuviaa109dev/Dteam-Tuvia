import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app import services
from app.config import settings
from app.models import Job, JobLog, JobStatus, utcnow
from app.queue import JobQueue
from app.worker.worker import Worker


def _seconds_until(iso: str) -> float:
    from datetime import datetime

    return (datetime.fromisoformat(iso) - utcnow()).total_seconds()


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---- completion -----------------------------------------------------------------------------


def test_job_completion_flow(client, submit, fetch, worker, queue):
    job = submit("email", {"to": "x@example.com", "subject": "Welcome"})

    assert worker.run_once() == job["id"]

    done = fetch(job["id"])
    assert done["status"] == "completed"
    assert done["attempts"] == 1
    assert done["progress"] == 100
    assert done["result"]["message_id"].endswith("@mail.mock>")
    assert done["result"]["to"] == "x@example.com"
    assert done["started_at"] and done["completed_at"]
    assert done["worker_id"] == "test-worker"
    assert queue.depth() == 0

    messages = [e["message"] for e in client.get(f"/jobs/{job['id']}/logs").json()]
    assert messages == ["job submitted", "job claimed", "job completed"]


@pytest.mark.parametrize("job_type,key", [("webhook", "status_code"), ("report", "file_url")])
def test_other_job_types_complete(submit, fetch, worker, job_type, key):
    job = submit(job_type)
    worker.run_once()
    assert fetch(job["id"])["status"] == "completed"
    assert key in fetch(job["id"])["result"]


def test_batch_job_tracks_progress(submit, fetch, worker, session_factory, monkeypatch):
    seen = []
    original = services.update_progress

    def spy(db, job_id, token, progress, cfg=settings):
        seen.append(progress)
        return original(db, job_id, token, progress, cfg)

    monkeypatch.setattr(services, "update_progress", spy)
    job = submit("batch", {"items": ["a", "b", "c", "d"], "item_delay_ms": 0, "fail_items": [2]})
    worker.run_once()

    done = fetch(job["id"])
    assert seen == [25, 50, 75, 100]
    assert done["status"] == "completed" and done["progress"] == 100
    assert done["result"]["total"] == 4
    assert done["result"]["succeeded"] == 3
    assert done["result"]["failures"][0]["index"] == 2


# ---- failure, retry with backoff, dead letter queue (corrupted data) ---------------------------


def test_failure_retries_with_exponential_backoff_then_fails_temporarily(client, submit, fetch, worker, queue,
                                                                         session_factory, make_due):
    job = submit("webhook", {"url": "https://hooks.example.com/fail", "failure_rate": 1.0})
    job_id = job["id"]

    # Attempt 1 runs immediately and fails -> retry scheduled ~30s later.
    assert worker.run_once() == job_id
    state = fetch(job_id)
    assert state["status"] == "scheduled"
    assert state["attempts"] == 1
    assert "503" in state["error"] and state["error_type"] == "JobError"
    assert 28 <= _seconds_until(state["run_at"]) <= 31

    # Not runnable before its time.
    with session_factory() as db:
        assert services.promote_due_jobs(db, queue) == 0
    assert worker.run_once() is None

    # Attempt 2 fails -> retry ~120s later.
    make_due(job_id)
    with session_factory() as db:
        assert services.promote_due_jobs(db, queue) == 1
    assert worker.run_once() == job_id
    state = fetch(job_id)
    assert state["status"] == "scheduled" and state["attempts"] == 2
    assert 118 <= _seconds_until(state["run_at"]) <= 121

    # Attempt 3 fails -> failed (temporarily). The data is fine, so it is NOT dead-lettered.
    make_due(job_id)
    with session_factory() as db:
        services.promote_due_jobs(db, queue)
    worker.run_once()
    state = fetch(job_id)
    assert state["status"] == "failed" and state["attempts"] == 3
    assert client.get("/dead-letter").json()["total"] == 0

    # Manual retry: back to pending with a fresh attempt budget.
    response = client.post(f"/jobs/{job_id}/retry")
    assert response.status_code == 200
    retried = response.json()
    assert retried["status"] == "pending" and retried["attempts"] == 0 and retried["error"] is None
    assert queue.contains(job_id)


def test_transient_failure_then_success(submit, fetch, worker, queue, session_factory, make_due):
    job = submit("webhook", {"url": "https://hooks.example.com/flaky", "failure_rate": 1.0})
    worker.run_once()
    assert fetch(job["id"])["status"] == "scheduled"

    # The downstream system "recovers".
    with session_factory() as db:
        db.execute(update(Job).where(Job.id == job["id"]).values(
            payload={"url": "https://hooks.example.com/flaky", "failure_rate": 0.0}))
        db.commit()
    make_due(job["id"])
    with session_factory() as db:
        services.promote_due_jobs(db, queue)
    worker.run_once()

    done = fetch(job["id"])
    assert done["status"] == "completed" and done["attempts"] == 2
    assert done["error"] is None


def test_poison_message_goes_straight_to_dead_letter(client, submit, worker, queue, session_factory):
    # A job whose payload cannot be processed (e.g. damaged after it was accepted).
    job = submit("email")
    with session_factory() as db:
        db.execute(update(Job).where(Job.id == job["id"]).values(payload={"garbage": True}))
        db.commit()

    worker.run_once()
    response = client.get(f"/jobs/{job['id']}")
    assert response.status_code == 404 and "dead letter queue" in response.json()["detail"]
    dead = client.get(f"/dead-letter/{job['id']}").json()
    assert dead["attempts"] == 1  # no pointless retries
    assert dead["error_type"] == "PermanentJobError"

    # Queue entries pointing at nonexistent jobs are dropped and counted.
    queue.enqueue("no-such-job", 0)
    assert worker.run_once() is None
    assert queue.depth() == 0
    assert queue.stats()["poison_dropped"] == 1


def test_job_timeout_is_enforced(submit, fetch, worker, monkeypatch):
    monkeypatch.setattr(settings, "sim_time_scale", 1.0)  # report really "works" for 3-5s
    job = submit("report", timeout_seconds=1)

    started = time.monotonic()
    worker.run_once()
    assert time.monotonic() - started < 2.5

    state = fetch(job["id"])
    assert state["status"] == "scheduled"  # timeouts are retryable
    assert state["error_type"] == "JobTimeoutError"


# ---- priority and scheduling -----------------------------------------------------------------


def test_priority_ordering(submit, worker):
    low = submit("email", priority=0)
    high_1 = submit("email", priority=10)
    mid = submit("email", priority=5)
    high_2 = submit("email", priority=10)
    urgent = submit("email", priority=100)

    order = [worker.run_once() for _ in range(5)]
    # Highest priority first; FIFO among equal priorities.
    assert order == [urgent["id"], high_1["id"], high_2["id"], mid["id"], low["id"]]


def test_scheduled_job_waits_until_due(submit, fetch, worker, queue, session_factory, make_due):
    run_at = (utcnow() + timedelta(hours=1)).isoformat()
    job = submit("email", scheduled_at=run_at)

    assert job["status"] == "scheduled"
    assert not queue.contains(job["id"])
    assert worker.run_once() is None
    with session_factory() as db:
        assert services.promote_due_jobs(db, queue) == 0

    make_due(job["id"])
    with session_factory() as db:
        assert services.promote_due_jobs(db, queue) == 1
    assert fetch(job["id"])["status"] == "pending"
    assert worker.run_once() == job["id"]
    assert fetch(job["id"])["status"] == "completed"


# ---- duplicate prevention and crash recovery -------------------------------------------------


def test_job_can_only_be_claimed_once(submit, session_factory):
    job = submit("email")
    with session_factory() as db:
        first = services.claim_job(db, job["id"], "worker-a")
    with session_factory() as db:
        second = services.claim_job(db, job["id"], "worker-b")
    assert first is not None and first.worker_id == "worker-a"
    assert second is None


def test_concurrent_workers_process_each_job_exactly_once(submit, session_factory, queue):
    jobs = [submit("email")["id"] for _ in range(20)]
    workers = [Worker(session_factory, queue, worker_id=f"w{i}", concurrency=3) for i in range(3)]
    for w in workers:
        w.start()
    try:
        def all_done():
            with session_factory() as db:
                return all(db.get(Job, j, populate_existing=True).status == "completed" for j in jobs)
        assert _wait_for(all_done, timeout=20)
    finally:
        for w in workers:
            w.stop()

    with session_factory() as db:
        for job_id in jobs:
            assert db.get(Job, job_id).attempts == 1
            claims = db.scalars(select(JobLog).where(JobLog.job_id == job_id, JobLog.message == "job claimed")).all()
            assert len(claims) == 1


def test_crashed_worker_job_is_recovered(submit, fetch, session_factory, queue, worker):
    job = submit("email")
    queue.pop()
    with session_factory() as db:  # a worker claims the job ... and then dies
        claimed = services.claim_job(db, job["id"], "doomed-worker")
        token = claimed.lease_token
    with session_factory() as db:
        assert services.heartbeat(db, job["id"], token)  # alive while it heartbeats

    # No reaping while the lease is valid.
    with session_factory() as db:
        assert services.reap_expired_leases(db, queue) == 0

    # Lease expires (no heartbeats) -> monitor recovers it as a failed attempt with backoff.
    with session_factory() as db:
        assert services.reap_expired_leases(db, queue, now=utcnow() + timedelta(seconds=settings.lease_seconds + 1)) == 1
    state = fetch(job["id"])
    assert state["status"] == "scheduled"
    assert state["error_type"] == "LeaseExpired"
    assert state["attempts"] == 1

    # The zombie worker can no longer write to the job (fencing token).
    with session_factory() as db:
        assert services.complete_job(db, job["id"], token, {"late": True}) is False
        assert services.heartbeat(db, job["id"], token) is False
    assert fetch(job["id"])["result"] is None


def test_crashed_worker_on_last_attempt_fails_without_dead_lettering(client, submit, fetch, session_factory, queue):
    # A crash says nothing about the data, so the job is failed (temporarily), not dead-lettered.
    job = submit("email", max_attempts=1)
    with session_factory() as db:
        services.claim_job(db, job["id"], "doomed-worker")
        services.reap_expired_leases(db, queue, now=utcnow() + timedelta(hours=1))
    assert fetch(job["id"])["status"] == "failed"
    assert client.get("/dead-letter").json()["total"] == 0


def test_reconciler_republishes_jobs_lost_from_redis(submit, fetch, session_factory, queue, worker):
    job = submit("email")
    queue.remove(job["id"])  # simulate Redis losing the entry
    assert worker.run_once() is None

    with session_factory() as db:
        later = utcnow() + timedelta(seconds=settings.reconcile_grace_seconds + 1)
        assert services.reconcile_pending(db, queue, now=later) == 1
        assert services.reconcile_pending(db, queue, now=later) == 0  # already queued
    assert worker.run_once() == job["id"]
    assert fetch(job["id"])["status"] == "completed"


# ---- graceful shutdown ------------------------------------------------------------------------


def test_graceful_shutdown_finishes_current_job(submit, fetch, session_factory, queue, monkeypatch):
    monkeypatch.setattr(settings, "sim_time_scale", 0.5)  # email takes 0.5-1.5s
    worker = Worker(session_factory, queue, worker_id="shutdown-test", concurrency=1)
    first = submit("email")
    worker.start()
    assert _wait_for(lambda: fetch(first["id"])["status"] == "processing", timeout=5)

    second = submit("email")
    stopper = threading.Thread(target=worker.stop)
    stopper.start()
    stopper.join(timeout=10)

    assert not stopper.is_alive()
    assert fetch(first["id"])["status"] == "completed"  # in-flight job finished
    assert fetch(second["id"])["status"] == "pending"  # no new work picked up
    assert queue.contains(second["id"])


def test_queue_score_orders_priority_then_fifo():
    assert JobQueue.score(10, 999) < JobQueue.score(5, 1) < JobQueue.score(5, 2) < JobQueue.score(-5, 1)
