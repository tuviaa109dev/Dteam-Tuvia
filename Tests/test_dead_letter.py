"""Dead letter queue: only jobs with corrupted data (unknown type, invalid payload) end up here.
They are moved out of the jobs table into dead_letter_jobs, where they can be inspected, fixed
and requeued, or discarded."""

import pytest

from app.config import settings


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setattr(settings, "dev_endpoints", True)


def _seed(client, count):
    response = client.post("/dev/corrupted-jobs", params={"count": count})
    assert response.status_code == 201, response.text
    return response.json()


def _drain(worker):
    while worker.run_once():
        pass


def test_corrupted_jobs_are_moved_to_the_dead_letter_table(client, worker, dev):
    seeded = _seed(client, 5)  # one of each corrupted sample, including an unknown job type
    assert {j["type"] for j in seeded} == {"email", "webhook", "batch", "fax", "report"}
    _drain(worker)

    listing = client.get("/dead-letter").json()
    assert listing["total"] == 5
    for job in seeded:
        # Gone from the jobs table, with a pointer to where it went.
        response = client.get(f"/jobs/{job['id']}")
        assert response.status_code == 404 and "dead letter queue" in response.json()["detail"]

        dead = client.get(f"/dead-letter/{job['id']}").json()
        assert dead["attempts"] == 1  # never retried
        assert dead["error_type"] == "PermanentJobError"
        assert dead["payload"] == job["payload"]  # kept exactly as it was, for inspection
        messages = [entry["message"] for entry in dead["logs"]]
        assert messages[0] == "dev: corrupted job injected (bypassed API validation)"
        assert "job claimed" in messages
        assert messages[-1] == "corrupted data: moved to dead letter queue"

    health = client.get("/health").json()
    assert health["queue"]["dead_letter"] == 5
    assert health["jobs_by_status"]["failed"] == 0
    assert client.get("/jobs").json()["total"] == 0


def test_temporary_failures_are_not_dead_lettered(client, submit, fetch, worker):
    job = submit("webhook", {"url": "https://hooks.example.com/down", "failure_rate": 1.0}, max_attempts=1)
    worker.run_once()
    assert fetch(job["id"])["status"] == "failed"
    assert client.get("/dead-letter").json()["total"] == 0


def test_requeue_needs_a_fixed_payload_then_the_job_runs(client, fetch, worker, queue, dev):
    [job] = _seed(client, 1)  # an email with no recipient
    _drain(worker)
    job_id = job["id"]

    refused = client.post(f"/dead-letter/{job_id}/requeue")
    assert refused.status_code == 409 and "still invalid" in refused.json()["detail"]

    fixed = {"to": "billing@example.com", "subject": "Invoice #1042"}
    response = client.post(f"/dead-letter/{job_id}/requeue", json={"payload": fixed})
    assert response.status_code == 201
    requeued = response.json()
    assert requeued["id"] == job_id  # same job, back in the jobs table
    assert requeued["status"] == "pending" and requeued["attempts"] == 0
    assert requeued["payload"]["to"] == "billing@example.com"
    assert client.get(f"/dead-letter/{job_id}").status_code == 404
    assert queue.contains(job_id)

    logs = [entry["message"] for entry in client.get(f"/jobs/{job_id}/logs").json()]
    assert logs[0] == "dev: corrupted job injected (bypassed API validation)"  # history restored
    assert logs[-1] == "requeued from dead letter queue"

    assert worker.run_once() == job_id
    assert fetch(job_id)["status"] == "completed"


def test_unknown_job_type_can_only_be_discarded(client, worker, dev):
    seeded = _seed(client, 4)
    _drain(worker)
    fax = next(j for j in seeded if j["type"] == "fax")

    response = client.post(f"/dead-letter/{fax['id']}/requeue", json={"payload": {"number": "1"}})
    assert response.status_code == 409 and "unknown job type" in response.json()["detail"]

    assert client.delete(f"/dead-letter/{fax['id']}").status_code == 204
    assert client.get(f"/dead-letter/{fax['id']}").status_code == 404
    assert client.delete(f"/dead-letter/{fax['id']}").status_code == 404


def test_filter_and_purge_dead_letters(client, worker, dev):
    _seed(client, 5)
    _drain(worker)

    emails = client.get("/dead-letter", params={"type": "email"}).json()
    assert emails["total"] == 1 and emails["items"][0]["type"] == "email"

    assert client.delete("/dead-letter", params={"type": "email"}).json() == {"deleted": 1}
    assert client.delete("/dead-letter").json() == {"deleted": 4}
    assert client.get("/dead-letter").json()["total"] == 0


def test_corrupted_job_injection_requires_dev_endpoints(client, monkeypatch):
    monkeypatch.setattr(settings, "dev_endpoints", False)
    assert client.post("/dev/corrupted-jobs").status_code == 403
