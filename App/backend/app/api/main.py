import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import services
from app.config import settings
from app.db import get_db
from app.logging_config import configure_logging
from app.queue import JobQueue
from app.schemas import (
    DeadLetterListOut, DeadLetterOut, HealthOut, JobCreate, JobListOut, JobLogOut, JobOut, JobType,
    RequeueIn, StatusName,
)

log = logging.getLogger("jobq.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    if getattr(app.state, "queue", None) is None:
        app.state.queue = JobQueue.from_url(settings.redis_url, settings.queue_prefix)
    yield


app = FastAPI(
    title="Job Queue Service",
    description="Submit background jobs, track their status and inspect results. "
    "Jobs are executed by separate worker processes.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
)


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if request.url.path != "/health":
        log.info("request", extra={
            "method": request.method, "path": request.url.path, "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
    return response


def get_queue(request: Request) -> JobQueue:
    return request.app.state.queue


DB = Annotated[Session, Depends(get_db)]
Queue = Annotated[JobQueue, Depends(get_queue)]


def _not_found(job_id: str, exc: Exception | None = None) -> HTTPException:
    if isinstance(exc, services.JobDeadLettered):
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"job {job_id} was moved to the dead letter queue (corrupted data): see /dead-letter/{job_id}",
        )
    return HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")


@app.post(
    "/jobs",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Idempotent replay: an existing job with this key was returned"}},
)
def submit_job(data: JobCreate, response: Response, db: DB, queue: Queue):
    job, created = services.submit_job(db, queue, data)
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    return job


@app.get("/jobs", response_model=JobListOut)
def list_jobs(
    db: DB,
    status_: Annotated[StatusName | None, Query(alias="status")] = None,
    type_: Annotated[JobType | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    items, total = services.list_jobs(db, status_, type_, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: DB):
    try:
        return services.get_job(db, job_id)
    except services.JobNotFound as exc:
        raise _not_found(job_id, exc) from None


@app.get("/jobs/{job_id}/logs", response_model=list[JobLogOut])
def get_job_logs(job_id: str, db: DB):
    try:
        return services.get_logs(db, job_id)
    except services.JobNotFound as exc:
        raise _not_found(job_id, exc) from None


@app.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: DB, queue: Queue):
    try:
        return services.cancel_job(db, queue, job_id)
    except services.JobNotFound as exc:
        raise _not_found(job_id, exc) from None
    except services.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@app.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: str, db: DB, queue: Queue):
    try:
        return services.retry_job(db, queue, job_id)
    except services.JobNotFound as exc:
        raise _not_found(job_id, exc) from None
    except services.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@app.post("/jobs/{job_id}/rerun", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def rerun_job(job_id: str, db: DB, queue: Queue):
    """Submit a new job with the same type, payload and settings as a finished one."""
    try:
        return services.rerun_job(db, queue, job_id)
    except services.JobNotFound as exc:
        raise _not_found(job_id, exc) from None
    except services.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


# ---- dead letter queue: jobs with corrupted data, kept in their own table for inspection ----


def _dead_letter_not_found(job_id: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"dead letter {job_id} not found")


@app.get("/dead-letter", response_model=DeadLetterListOut)
def list_dead_letters(
    db: DB,
    type_: Annotated[str | None, Query(alias="type")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    items, total = services.list_dead_letters(db, type_, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/dead-letter/{job_id}", response_model=DeadLetterOut)
def get_dead_letter(job_id: str, db: DB):
    try:
        return services.get_dead_letter(db, job_id)
    except services.DeadLetterNotFound:
        raise _dead_letter_not_found(job_id) from None


@app.post("/dead-letter/{job_id}/requeue", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def requeue_dead_letter(job_id: str, db: DB, queue: Queue, body: RequeueIn | None = None):
    """Move a dead letter back into the jobs table as pending. Send `{"payload": {...}}` to fix the
    corrupted data first. 409 if the payload is still invalid or the job type is unknown."""
    try:
        return services.requeue_dead_letter(db, queue, job_id, body.payload if body else None)
    except services.DeadLetterNotFound:
        raise _dead_letter_not_found(job_id) from None
    except services.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@app.delete("/dead-letter/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard_dead_letter(job_id: str, db: DB):
    try:
        services.discard_dead_letter(db, job_id)
    except services.DeadLetterNotFound:
        raise _dead_letter_not_found(job_id) from None


@app.delete("/dead-letter")
def purge_dead_letters(db: DB, type_: Annotated[str | None, Query(alias="type")] = None):
    """Discard every dead letter (optionally only one job type)."""
    return {"deleted": services.purge_dead_letters(db, type_)}


# ---- dev tools (DEV_ENDPOINTS=true only) -----------------------------------------------------


def _require_dev_endpoints() -> None:
    if not settings.dev_endpoints:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "dev endpoints are disabled (set DEV_ENDPOINTS=true)")


@app.post("/dev/reset", responses={403: {"description": "Dev endpoints are disabled"}})
def dev_reset(db: DB, queue: Queue):
    """Delete all jobs, logs, idempotency keys, dead letters and queued work."""
    _require_dev_endpoints()
    return services.reset_all_data(db, queue)


@app.post(
    "/dev/corrupted-jobs",
    response_model=list[JobOut],
    status_code=status.HTTP_201_CREATED,
    responses={403: {"description": "Dev endpoints are disabled"}},
)
def dev_corrupted_jobs(db: DB, queue: Queue, count: Annotated[int, Query(ge=1, le=50)] = 3):
    """Inject pending jobs with corrupted data, bypassing validation. Workers dead-letter them."""
    _require_dev_endpoints()
    return services.seed_corrupted_jobs(db, queue, count)


@app.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}})
def health(response: Response, db: DB, queue: Queue):
    db_ok = redis_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        log.warning("health: database unavailable", exc_info=True)
    try:
        redis_ok = queue.ping()
    except Exception:
        log.warning("health: redis unavailable", exc_info=True)

    body: dict = {"status": "ok" if db_ok and redis_ok else "degraded", "database": db_ok, "redis": redis_ok}
    if db_ok and redis_ok:
        body.update(services.queue_stats(db, queue))
    else:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return body
