"""Test fixtures.

By default tests run against SQLite + fakeredis, so `pytest` works with no services running.
Set TEST_DATABASE_URL / TEST_REDIS_URL to run the same suite against real PostgreSQL and Redis
(the `tests` service in docker-compose does this).
"""

import os
import random
import sys
from datetime import timedelta
from pathlib import Path

# Make the `app` package importable when running `pytest Tests` from the repo root.
# (In the Docker test image the package sits next to Tests/ and is already importable.)
_BACKEND = Path(__file__).resolve().parent.parent / "App" / "backend"
if _BACKEND.is_dir():
    sys.path.insert(0, str(_BACKEND))

import fakeredis
import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.api.main import app, get_queue
from app.config import settings
from app.db import Base, get_db, make_engine
from app.models import Job, utcnow
from app.queue import JobQueue
from app.worker.worker import Worker


@pytest.fixture(autouse=True)
def fast_settings(monkeypatch):
    monkeypatch.setattr(settings, "sim_time_scale", 0.0)  # mock jobs finish instantly
    monkeypatch.setattr(settings, "retry_base_delay", 30.0)
    monkeypatch.setattr(settings, "retry_backoff_factor", 4.0)
    monkeypatch.setattr(settings, "default_max_attempts", 3)
    monkeypatch.setattr(settings, "heartbeat_interval", 0.2)
    monkeypatch.setattr(settings, "poll_timeout", 0.1)
    monkeypatch.setattr(settings, "maintenance_interval", 0.1)


@pytest.fixture
def engine(tmp_path):
    url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    eng = make_engine(url)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def queue():
    url = os.getenv("TEST_REDIS_URL")
    client = Redis.from_url(url, decode_responses=True) if url else fakeredis.FakeRedis(decode_responses=True)
    client.flushdb()
    yield JobQueue(client, prefix="test")
    client.flushdb()


@pytest.fixture
def client(session_factory, queue):
    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_queue] = lambda: queue
    app.state.queue = queue
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    app.state.queue = None


@pytest.fixture
def worker(session_factory, queue):
    return Worker(session_factory, queue, worker_id="test-worker", concurrency=1, rng=random.Random(7))


@pytest.fixture
def submit(client):
    def _submit(job_type="email", payload=None, expected=201, **fields):
        defaults = {
            "email": {"to": "user@example.com", "subject": "Hello"},
            "webhook": {"url": "https://hooks.example.com/x", "failure_rate": 0.0},
            "report": {"report_type": "sales"},
            "batch": {"items": [1, 2, 3, 4], "item_delay_ms": 0},
        }
        body = {"type": job_type, "payload": payload if payload is not None else defaults[job_type], **fields}
        response = client.post("/jobs", json=body)
        assert response.status_code == expected, response.text
        return response.json()

    return _submit


@pytest.fixture
def fetch(client):
    def _fetch(job_id):
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200, response.text
        return response.json()

    return _fetch


@pytest.fixture
def make_due(session_factory):
    """Fast-forward a scheduled job: move its run_at into the past."""

    def _make_due(job_id):
        with session_factory() as session:
            session.execute(update(Job).where(Job.id == job_id).values(run_at=utcnow() - timedelta(seconds=1)))
            session.commit()

    return _make_due
