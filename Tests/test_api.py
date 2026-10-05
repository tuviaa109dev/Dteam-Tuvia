from datetime import timedelta

from sqlalchemy import func, select, update

from app.models import IdempotencyKey, Job, utcnow


def test_submit_and_retrieve_job(client, submit, fetch, queue):
    job = submit("email", {"to": "a@example.com", "subject": "Hi"}, priority=7)

    assert job["status"] == "pending"
    assert job["priority"] == 7
    assert job["attempts"] == 0 and job["max_attempts"] == 3
    assert job["payload"] == {"to": "a@example.com", "subject": "Hi", "body": ""}  # defaults applied
    assert queue.contains(job["id"])

    fetched = fetch(job["id"])
    assert fetched["id"] == job["id"] and fetched["type"] == "email"

    logs = client.get(f"/jobs/{job['id']}/logs").json()
    assert [entry["message"] for entry in logs] == ["job submitted"]

    assert client.get("/jobs/does-not-exist").status_code == 404


def test_submission_validates_payload(client, submit):
    response = client.post("/jobs", json={"type": "email", "payload": {"to": "not-an-email"}})
    assert response.status_code == 422
    response = client.post("/jobs", json={"type": "teleport", "payload": {}})
    assert response.status_code == 422
    response = client.post("/jobs", json={"type": "report", "payload": {"report_type": "x"}, "priority": 1000})
    assert response.status_code == 422


def test_list_jobs_with_filters(client, submit):
    email = submit("email")
    report = submit("report")
    scheduled = submit("webhook", delay_seconds=3600)

    all_jobs = client.get("/jobs").json()
    assert all_jobs["total"] == 3

    by_type = client.get("/jobs", params={"type": "report"}).json()
    assert [j["id"] for j in by_type["items"]] == [report["id"]]

    by_status = client.get("/jobs", params={"status": "scheduled"}).json()
    assert [j["id"] for j in by_status["items"]] == [scheduled["id"]]

    both = client.get("/jobs", params={"status": "pending", "type": "email"}).json()
    assert [j["id"] for j in both["items"]] == [email["id"]]

    page = client.get("/jobs", params={"limit": 2, "offset": 2}).json()
    assert len(page["items"]) == 1 and page["total"] == 3

    assert client.get("/jobs", params={"status": "bogus"}).status_code == 422


def test_cancel_pending_and_scheduled_jobs(client, submit, fetch, queue, worker):
    pending = submit("email")
    scheduled = submit("email", delay_seconds=600)

    for job in (pending, scheduled):
        response = client.post(f"/jobs/{job['id']}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["completed_at"] is not None

    assert not queue.contains(pending["id"])
    assert worker.run_once() is None  # nothing left to run
    assert fetch(pending["id"])["status"] == "cancelled"

    # Cancelling twice, or cancelling a finished job, is a conflict.
    assert client.post(f"/jobs/{pending['id']}/cancel").status_code == 409
    done = submit("email")
    worker.run_once()
    assert fetch(done["id"])["status"] == "completed"
    assert client.post(f"/jobs/{done['id']}/cancel").status_code == 409
    assert client.post("/jobs/nope/cancel").status_code == 404


def test_cancel_wins_over_stale_queue_entry(client, submit, fetch, queue, worker):
    """A job cancelled after a worker popped its ID must not run."""
    job = submit("email")
    popped_id, _ = queue.pop()
    client.post(f"/jobs/{job['id']}/cancel")

    assert worker.process(popped_id) is None
    assert fetch(job["id"])["status"] == "cancelled"
    assert fetch(job["id"])["attempts"] == 0


def test_idempotency_returns_existing_job(client, submit, db):
    first = client.post("/jobs", json={
        "type": "email", "payload": {"to": "a@example.com", "subject": "x"}, "idempotency_key": "order-42",
    })
    second = client.post("/jobs", json={
        "type": "email", "payload": {"to": "b@example.com", "subject": "y"}, "idempotency_key": "order-42",
    })

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["payload"]["to"] == "a@example.com"
    assert db.scalar(select(func.count()).select_from(Job)) == 1

    key = db.get(IdempotencyKey, "order-42")
    ttl = key.expires_at - key.created_at
    assert ttl >= timedelta(hours=24)


def test_idempotency_key_expires_after_ttl(client, submit, db):
    first = submit("email", idempotency_key="k1")
    db.execute(update(IdempotencyKey).values(expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()

    second = submit("email", idempotency_key="k1")  # expected=201: a new job
    assert second["id"] != first["id"]


def test_retry_endpoint_only_accepts_failed_jobs(client, submit):
    job = submit("email")
    response = client.post(f"/jobs/{job['id']}/retry")
    assert response.status_code == 409
    assert "only failed jobs" in response.json()["detail"]


def test_rerun_creates_a_copy_of_a_finished_job(client, submit, fetch, worker, queue):
    original = submit("report", {"report_type": "sales", "format": "csv"}, priority=4, max_attempts=2)
    assert client.post(f"/jobs/{original['id']}/rerun").status_code == 409  # still pending

    worker.run_once()
    response = client.post(f"/jobs/{original['id']}/rerun")
    assert response.status_code == 201
    copy = response.json()

    assert copy["id"] != original["id"]
    assert copy["status"] == "pending" and copy["attempts"] == 0
    assert (copy["type"], copy["payload"], copy["priority"], copy["max_attempts"]) == (
        "report", original["payload"], 4, 2)
    assert queue.contains(copy["id"])
    assert fetch(original["id"])["status"] == "completed"  # original untouched
    copy_logs = client.get(f"/jobs/{copy['id']}/logs").json()
    assert copy_logs[0]["metadata"]["rerun_of"] == original["id"]

    cancelled = submit("email")
    client.post(f"/jobs/{cancelled['id']}/cancel")
    assert client.post(f"/jobs/{cancelled['id']}/rerun").status_code == 201
    assert client.post("/jobs/nope/rerun").status_code == 404


def test_dev_reset_wipes_everything_only_when_enabled(client, submit, worker, queue, monkeypatch):
    from app.config import settings

    submit("email", idempotency_key="k")
    submit("email", delay_seconds=60)
    submit("webhook", {"url": "https://x.io", "failure_rate": 1.0}, max_attempts=1)
    monkeypatch.setattr(settings, "dev_endpoints", True)
    assert client.post("/dev/corrupted-jobs", params={"count": 1}).status_code == 201
    while worker.run_once():
        pass
    assert client.get("/dead-letter").json()["total"] == 1

    monkeypatch.setattr(settings, "dev_endpoints", False)
    assert client.post("/dev/reset").status_code == 403
    assert client.get("/jobs").json()["total"] == 3

    monkeypatch.setattr(settings, "dev_endpoints", True)
    response = client.post("/dev/reset")
    assert response.status_code == 200
    assert response.json() == {"deleted_jobs": 3, "deleted_dead_letters": 1}
    assert client.get("/jobs").json()["total"] == 0
    assert client.get("/dead-letter").json()["total"] == 0
    assert queue.depth() == 0
    # The idempotency key is gone too, so it can create a new job.
    assert submit("email", idempotency_key="k")["status"] == "pending"


def test_health_reports_queue_statistics(client, submit):
    submit("email", priority=1)
    submit("email", delay_seconds=300)
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["database"] and body["redis"]
    assert body["queue"]["ready"] == 1
    assert body["queue"]["scheduled"] == 1
    assert body["queue"]["dead_letter"] == 0
    assert body["jobs_by_status"]["pending"] == 1
    assert isinstance(body["workers"], list)
