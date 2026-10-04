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
from app.schemas import HealthOut, JobCreate, JobListOut, JobLogOut, JobOut, JobType, StatusName

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


def _not_found(job_id: str) -> HTTPException:
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
    except services.JobNotFound:
        raise _not_found(job_id) from None


@app.get("/jobs/{job_id}/logs", response_model=list[JobLogOut])
def get_job_logs(job_id: str, db: DB):
    try:
        return services.get_logs(db, job_id)
    except services.JobNotFound:
        raise _not_found(job_id) from None


@app.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: DB, queue: Queue):
    try:
        return services.cancel_job(db, queue, job_id)
    except services.JobNotFound:
        raise _not_found(job_id) from None
    except services.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@app.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: str, db: DB, queue: Queue):
    try:
        return services.retry_job(db, queue, job_id)
    except services.JobNotFound:
        raise _not_found(job_id) from None
    except services.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@app.post("/jobs/{job_id}/rerun", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def rerun_job(job_id: str, db: DB, queue: Queue):
    """Submit a new job with the same type, payload and settings as a finished one."""
    try:
        return services.rerun_job(db, queue, job_id)
    except services.JobNotFound:
        raise _not_found(job_id) from None
    except services.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@app.get("/dead-letter", response_model=list[JobOut])
def dead_letter_queue(db: DB, queue: Queue, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    return services.dead_letter_jobs(db, queue, limit)


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
