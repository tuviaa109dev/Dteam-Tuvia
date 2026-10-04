"""Job state machine. Every transition is a conditional UPDATE (or SELECT ... FOR UPDATE) on
the jobs table, so concurrent API calls, workers and maintenance passes cannot race each other
into an invalid state. Redis is touched only after the DB commit, and Redis failures are
tolerated because the reconciler re-publishes any pending job missing from the queue."""

import logging
from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models import IdempotencyKey, Job, JobLog, JobStatus, LogLevel, utcnow
from app.queue import JobQueue
from app.schemas import JobCreate

log = logging.getLogger("jobq.service")

CANCELLABLE = (JobStatus.PENDING, JobStatus.SCHEDULED)


class JobNotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


def add_log(db: Session, job_id: str, level: str, message: str, **metadata) -> None:
    db.add(JobLog(job_id=job_id, level=level, message=message, metadata_=metadata))


def _reload(db: Session, job_id: str) -> Job | None:
    return db.get(Job, job_id, populate_existing=True)


def _safe(action: str, job_id: str, fn, *args) -> None:
    """Run a Redis side effect; on failure, log and rely on the reconciler."""
    try:
        fn(*args)
    except RedisError:
        log.warning("redis %s failed; reconciler will repair", action, extra={"job_id": job_id}, exc_info=True)


# ---- API-side operations ---------------------------------------------------------------------


def _find_by_idempotency_key(db: Session, key: str, now: datetime) -> Job | None:
    return db.scalar(
        select(Job)
        .join(IdempotencyKey, IdempotencyKey.job_id == Job.id)
        .where(IdempotencyKey.key == key, IdempotencyKey.expires_at > now)
    )


def submit_job(
    db: Session, queue: JobQueue, data: JobCreate, cfg: Settings = settings, rerun_of: str | None = None
) -> tuple[Job, bool]:
    """Create a job. Returns (job, created); created is False for an idempotent replay."""
    now = utcnow()
    key = data.idempotency_key
    if key:
        existing = _find_by_idempotency_key(db, key, now)
        if existing:
            return existing, False
        # An expired key may be reused for a new job.
        db.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.expires_at <= now))

    if data.scheduled_at is not None:
        run_at = max(data.scheduled_at, now)
    elif data.delay_seconds:
        run_at = now + timedelta(seconds=data.delay_seconds)
    else:
        run_at = now
    status = JobStatus.SCHEDULED if run_at > now else JobStatus.PENDING

    job = Job(
        id=str(uuid4()),
        type=data.type,
        payload=data.payload,
        status=status,
        priority=data.priority,
        attempts=0,
        max_attempts=data.max_attempts or cfg.default_max_attempts,
        timeout_seconds=data.timeout_seconds or cfg.default_job_timeout,
        progress=0,
        scheduled_at=run_at if status == JobStatus.SCHEDULED else None,
        run_at=run_at,
        created_at=now,
        updated_at=now,
        idempotency_key=key,
    )
    db.add(job)
    db.flush()  # the job row must exist before the idempotency key that references it
    if key:
        db.add(IdempotencyKey(
            key=key, job_id=job.id, created_at=now,
            expires_at=now + timedelta(hours=cfg.idempotency_ttl_hours),
        ))
    extra = {"rerun_of": rerun_of} if rerun_of else {}
    add_log(db, job.id, LogLevel.INFO, "job submitted", status=status, priority=data.priority,
            run_at=run_at.isoformat(), **extra)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request inserted the same idempotency key first: return its job.
        db.rollback()
        existing = _find_by_idempotency_key(db, key, utcnow()) if key else None
        if existing:
            return existing, False
        raise

    log.info("job submitted", extra={"job_id": job.id, "job_type": job.type, "status": status})
    if status == JobStatus.PENDING:
        _safe("enqueue", job.id, queue.enqueue, job.id, job.priority)
    return job, True


def get_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise JobNotFound(job_id)
    return job


def list_jobs(
    db: Session, status: str | None = None, job_type: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[Job], int]:
    query = select(Job)
    if status:
        query = query.where(Job.status == status)
    if job_type:
        query = query.where(Job.type == job_type)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Job.created_at.desc(), Job.id).offset(offset).limit(limit)).all()
    return list(items), int(total or 0)


def get_logs(db: Session, job_id: str) -> list[JobLog]:
    get_job(db, job_id)
    return list(db.scalars(select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.id)))


def cancel_job(db: Session, queue: JobQueue, job_id: str) -> Job:
    now = utcnow()
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(CANCELLABLE))
        .values(status=JobStatus.CANCELLED, completed_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        job = get_job(db, job_id)
        raise InvalidTransition(f"only pending or scheduled jobs can be cancelled (job is {job.status})")
    add_log(db, job_id, LogLevel.INFO, "job cancelled")
    db.commit()
    _safe("remove", job_id, queue.remove, job_id)
    log.info("job cancelled", extra={"job_id": job_id})
    return _reload(db, job_id)


def retry_job(db: Session, queue: JobQueue, job_id: str) -> Job:
    """Manual retry of a permanently failed job: FAILED -> PENDING with a fresh attempt budget."""
    now = utcnow()
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.FAILED)
        .values(
            status=JobStatus.PENDING, attempts=0, progress=0, run_at=now,
            error=None, error_type=None, result=None,
            started_at=None, completed_at=None, dead_lettered_at=None, worker_id=None,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        job = get_job(db, job_id)
        raise InvalidTransition(f"only failed jobs can be retried (job is {job.status})")
    add_log(db, job_id, LogLevel.INFO, "manual retry requested")
    db.commit()
    job = _reload(db, job_id)
    _safe("dlq-remove", job_id, queue.remove_dead_letter, job_id)
    _safe("enqueue", job_id, queue.enqueue, job_id, job.priority)
    log.info("job manually retried", extra={"job_id": job_id})
    return job


RERUNNABLE = (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED)


def rerun_job(db: Session, queue: JobQueue, job_id: str, cfg: Settings = settings) -> Job:
    """Submit a fresh copy of a finished job (same type, payload, priority, limits).
    Unlike retry_job, the original job and its result are left untouched."""
    original = get_job(db, job_id)
    if original.status not in RERUNNABLE:
        raise InvalidTransition(f"only completed, cancelled or failed jobs can be rerun (job is {original.status})")
    try:
        data = JobCreate(
            type=original.type, payload=original.payload, priority=original.priority,
            max_attempts=original.max_attempts, timeout_seconds=original.timeout_seconds,
        )
    except ValidationError as exc:
        raise InvalidTransition(f"job payload is no longer valid: {exc.errors()[0]['msg']}") from None
    job, _ = submit_job(db, queue, data, cfg, rerun_of=job_id)
    add_log(db, job_id, LogLevel.INFO, "rerun requested", new_job_id=job.id)
    db.commit()
    log.info("job rerun", extra={"job_id": job_id, "new_job_id": job.id})
    return job


def reset_all_data(db: Session, queue: JobQueue) -> int:
    """Dev only: delete every job, log and idempotency key, and empty the Redis queue/DLQ.
    Workers mid-job simply lose their lease (their fenced writes match no row) and move on."""
    deleted = db.scalar(select(func.count()).select_from(Job)) or 0
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("TRUNCATE job_logs, idempotency_keys, jobs RESTART IDENTITY"))
    else:
        for model in (JobLog, IdempotencyKey, Job):
            db.execute(delete(model))
    db.commit()
    queue.reset()
    log.warning("all job data reset", extra={"jobs_deleted": deleted})
    return deleted


def queue_stats(db: Session, queue: JobQueue) -> dict:
    counts = {s.value: 0 for s in JobStatus}
    counts.update(dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all()))
    oldest = db.scalar(select(func.min(Job.run_at)).where(Job.status == JobStatus.PENDING))
    return {
        "jobs_by_status": counts,
        "queue": {
            "ready": queue.depth(),
            "scheduled": counts[JobStatus.SCHEDULED],
            "processing": counts[JobStatus.PROCESSING],
            "dead_letter": queue.dlq_size(),
            "oldest_pending_age_seconds": round((utcnow() - oldest).total_seconds(), 3) if oldest else None,
            "poison_messages_dropped": queue.stats().get("poison_dropped", 0),
        },
        "workers": queue.active_workers(),
    }


# ---- worker-side operations ------------------------------------------------------------------


def claim_job(db: Session, job_id: str, worker_id: str, cfg: Settings = settings) -> Job | None:
    """Atomically take ownership of a job: PENDING -> PROCESSING with a fresh lease + token.

    The WHERE clause makes this a compare-and-set: if two workers somehow hold the same ID,
    only one UPDATE matches the row. Returns None if the job is gone, cancelled or taken.
    """
    now = utcnow()
    token = str(uuid4())
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.PENDING, Job.run_at <= now)
        .values(
            status=JobStatus.PROCESSING,
            attempts=Job.attempts + 1,
            worker_id=worker_id,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=cfg.lease_seconds),
            started_at=now,
            progress=0,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    job = _reload(db, job_id)
    add_log(db, job_id, LogLevel.INFO, "job claimed", worker_id=worker_id, attempt=job.attempts)
    db.commit()
    return job


def heartbeat(db: Session, job_id: str, token: str, cfg: Settings = settings) -> bool:
    """Extend the lease. False means the lease was lost (reaped) and the worker must stop."""
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_token == token, Job.status == JobStatus.PROCESSING)
        .values(lease_expires_at=utcnow() + timedelta(seconds=cfg.lease_seconds))
    )
    db.commit()
    return result.rowcount == 1


def update_progress(db: Session, job_id: str, token: str, progress: int, cfg: Settings = settings) -> bool:
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_token == token, Job.status == JobStatus.PROCESSING)
        .values(progress=max(0, min(100, progress)),
                lease_expires_at=utcnow() + timedelta(seconds=cfg.lease_seconds))
    )
    db.commit()
    return result.rowcount == 1


def complete_job(db: Session, job_id: str, token: str, result: dict) -> bool:
    now = utcnow()
    res = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_token == token, Job.status == JobStatus.PROCESSING)
        .values(
            status=JobStatus.COMPLETED, result=result, progress=100, completed_at=now,
            error=None, error_type=None, lease_token=None, lease_expires_at=None,
        )
    )
    if res.rowcount != 1:
        db.rollback()
        return False
    add_log(db, job_id, LogLevel.INFO, "job completed")
    db.commit()
    return True


def _apply_failure(
    db: Session, job: Job, error: str, error_type: str, retryable: bool, now: datetime, cfg: Settings
) -> bool:
    """Mutate a locked PROCESSING job after a failed attempt. Returns True if dead-lettered."""
    job.error = error[:4000]
    job.error_type = error_type
    job.lease_token = None
    job.lease_expires_at = None
    if retryable and job.attempts < job.max_attempts:
        delay = cfg.retry_delay(job.attempts)
        job.status = JobStatus.SCHEDULED
        job.run_at = now + timedelta(seconds=delay)
        add_log(db, job.id, LogLevel.WARNING, f"attempt {job.attempts} failed; retrying in {delay:g}s",
                error=job.error, error_type=error_type, attempt=job.attempts, retry_at=job.run_at.isoformat())
        return False
    job.status = JobStatus.FAILED
    job.completed_at = now
    job.dead_lettered_at = now
    reason = "max attempts reached" if retryable else "non-retryable error"
    add_log(db, job.id, LogLevel.ERROR, f"job failed permanently ({reason}); moved to dead letter queue",
            error=job.error, error_type=error_type, attempt=job.attempts)
    return True


def fail_job(
    db: Session, queue: JobQueue, job_id: str, token: str, error: str, error_type: str,
    retryable: bool = True, cfg: Settings = settings,
) -> str | None:
    """Record a failed attempt. Returns the new status, or None if the lease was already lost."""
    job = db.scalar(
        select(Job)
        .where(Job.id == job_id, Job.lease_token == token, Job.status == JobStatus.PROCESSING)
        .with_for_update()
    )
    if job is None:
        db.rollback()
        return None
    dead = _apply_failure(db, job, error, error_type, retryable, utcnow(), cfg)
    status = job.status
    db.commit()
    if dead:
        _safe("dead-letter", job_id, queue.dead_letter, job_id)
    return status


# ---- maintenance (scheduler, reaper, reconciler) ---------------------------------------------


def promote_due_jobs(db: Session, queue: JobQueue, now: datetime | None = None, limit: int = 500) -> int:
    """SCHEDULED -> PENDING for jobs whose run_at has arrived (future jobs and retry backoff)."""
    now = now or utcnow()
    rows = db.execute(
        select(Job.id, Job.priority)
        .where(Job.status == JobStatus.SCHEDULED, Job.run_at <= now)
        .order_by(Job.run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    if not rows:
        db.rollback()
        return 0
    ids = [r.id for r in rows]
    db.execute(update(Job).where(Job.id.in_(ids), Job.status == JobStatus.SCHEDULED)
               .values(status=JobStatus.PENDING))
    db.commit()
    for job_id, priority in rows:
        _safe("enqueue", job_id, queue.enqueue, job_id, priority)
    log.info("promoted due jobs", extra={"count": len(rows)})
    return len(rows)


def reap_expired_leases(
    db: Session, queue: JobQueue, now: datetime | None = None, cfg: Settings = settings, limit: int = 100
) -> int:
    """Recover jobs whose worker stopped heartbeating (crashed, killed, hung past its timeout).
    The expired attempt counts as a failure and follows the normal retry/backoff policy."""
    now = now or utcnow()
    jobs = db.scalars(
        select(Job)
        .where(Job.status == JobStatus.PROCESSING, Job.lease_expires_at < now)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    if not jobs:
        db.rollback()
        return 0
    dead_lettered = []
    for job in jobs:
        log.warning("lease expired; recovering job",
                    extra={"job_id": job.id, "worker_id": job.worker_id, "attempt": job.attempts})
        if _apply_failure(db, job, f"lease expired: worker {job.worker_id} stopped heartbeating",
                          "LeaseExpired", True, now, cfg):
            dead_lettered.append(job.id)
    db.commit()
    for job_id in dead_lettered:
        _safe("dead-letter", job_id, queue.dead_letter, job_id)
    return len(jobs)


def reconcile_pending(
    db: Session, queue: JobQueue, now: datetime | None = None, cfg: Settings = settings, limit: int = 1000
) -> int:
    """Re-publish PENDING jobs missing from Redis (enqueue failed, Redis data lost, or a worker
    died between pop and claim). ZADD NX keeps existing entries untouched, and the conditional
    claim makes any duplicate entry harmless."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=cfg.reconcile_grace_seconds)
    rows = db.execute(
        select(Job.id, Job.priority)
        .where(Job.status == JobStatus.PENDING, Job.updated_at <= cutoff)
        .order_by(Job.priority.desc(), Job.run_at)
        .limit(limit)
    ).all()
    db.rollback()
    restored = sum(1 for job_id, priority in rows if queue.enqueue(job_id, priority))
    if restored:
        log.warning("re-enqueued pending jobs missing from queue", extra={"count": restored})
    return restored


def purge_expired_idempotency_keys(db: Session, now: datetime | None = None) -> int:
    result = db.execute(delete(IdempotencyKey).where(IdempotencyKey.expires_at <= (now or utcnow())))
    db.commit()
    return result.rowcount


def run_maintenance(db: Session, queue: JobQueue, cfg: Settings = settings) -> dict[str, int]:
    return {
        "reaped": reap_expired_leases(db, queue, cfg=cfg),
        "promoted": promote_due_jobs(db, queue),
        "reconciled": reconcile_pending(db, queue, cfg=cfg),
        "idempotency_keys_purged": purge_expired_idempotency_keys(db),
    }
