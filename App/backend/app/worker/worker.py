"""Worker process: N slot threads pull job IDs from Redis, claim them in PostgreSQL and run
them; a heartbeat thread per running job keeps its lease alive; one maintenance thread (leader-
elected via a short Redis lock) promotes scheduled jobs, reaps expired leases and reconciles."""

import contextvars
import logging
import os
import random
import socket
import threading
import time
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app import services
from app.config import Settings, settings
from app.logging_config import log_context
from app.models import Job
from app.queue import JobQueue
from app.worker.handlers import JobContext, JobError, LeaseLostError, execute

log = logging.getLogger("jobq.worker")


class _Heartbeat:
    """Extends the job's lease every `heartbeat_interval` seconds. Stops renewing once the job
    is past its timeout (+grace), so even a handler stuck in non-cooperative code is eventually
    reaped and retried elsewhere."""

    def __init__(self, worker: "Worker", job_id: str, token: str, ctx: JobContext):
        self.worker, self.job_id, self.token, self.ctx = worker, job_id, token, ctx
        self._done = threading.Event()
        run = contextvars.copy_context().run  # keep job log context in this thread
        self._thread = threading.Thread(target=run, args=(self._run,), name=f"hb-{job_id[:8]}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        cfg = self.worker.cfg
        while not self._done.wait(cfg.heartbeat_interval):
            if time.monotonic() > self.ctx.deadline + cfg.timeout_grace_seconds:
                log.error("job overran its timeout; no longer renewing lease")
                return
            try:
                with self.worker.session_factory() as db:
                    alive = services.heartbeat(db, self.job_id, self.token, cfg)
            except Exception:
                log.warning("heartbeat failed", exc_info=True)
                continue
            if not alive:
                log.warning("lease lost; signalling job to abort")
                self.ctx.lease_lost.set()
                return


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        queue: JobQueue,
        cfg: Settings = settings,
        worker_id: str | None = None,
        concurrency: int | None = None,
        rng: random.Random | None = None,
    ):
        self.session_factory = session_factory
        self.queue = queue
        self.cfg = cfg
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
        self.concurrency = concurrency or cfg.worker_concurrency
        self.rng = rng or random.Random()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active: dict[str, str] = {}
        self._active_lock = threading.Lock()

    # ---- processing one job ------------------------------------------------------------------

    def run_once(self, timeout: float = 0) -> str | None:
        """Pop one job and process it. Returns the job ID if it was executed, else None."""
        popped = self.queue.pop(timeout)
        if popped is None:
            return None
        job_id, score = popped
        if self._stop.is_set():
            # Shutdown began while we were blocked on the queue: hand the job back untouched.
            self.queue.requeue(job_id, score)
            return None
        return self.process(job_id)

    def process(self, job_id: str) -> str | None:
        with self.session_factory() as db:
            job = services.claim_job(db, job_id, self.worker_id, self.cfg)
            if job is None:
                self._drop_unclaimable(db, job_id)
                return None
            job_type, payload, attempt = job.type, dict(job.payload), job.attempts
            token, timeout = job.lease_token, job.timeout_seconds

        with log_context(job_id=job_id, job_type=job_type, attempt=attempt, worker_id=self.worker_id):
            ctx = JobContext(
                job_id, attempt, timeout, time_scale=self.cfg.sim_time_scale, rng=self.rng,
                on_progress=lambda pct: self._report_progress(job_id, token, pct, ctx),
            )
            heartbeat = _Heartbeat(self, job_id, token, ctx)
            self._set_active(job_id, True)
            heartbeat.start()
            log.info("job started")
            started = time.monotonic()
            try:
                result = execute(ctx, job_type, payload)
            except LeaseLostError:
                log.warning("lease lost during execution; result discarded")
            except JobError as exc:
                self._fail(job_id, token, str(exc), type(exc).__name__, exc.retryable)
            except Exception as exc:  # bug in a handler: treat as a retryable failure
                log.exception("unexpected handler error")
                self._fail(job_id, token, f"{type(exc).__name__}: {exc}", type(exc).__name__, True)
            else:
                with self.session_factory() as db:
                    if services.complete_job(db, job_id, token, result):
                        log.info("job completed", extra={"duration_ms": round((time.monotonic() - started) * 1000)})
                    else:
                        log.warning("completion rejected: lease no longer held")
            finally:
                heartbeat.stop()
                self._set_active(job_id, False)
        return job_id

    def _fail(self, job_id: str, token: str, error: str, error_type: str, retryable: bool) -> None:
        with self.session_factory() as db:
            outcome = services.fail_job(db, self.queue, job_id, token, error, error_type, retryable, self.cfg)
        level = logging.ERROR if outcome in ("failed", services.DEAD_LETTER) else logging.WARNING
        log.log(level, "job attempt failed", extra={"error": error, "error_type": error_type, "outcome": outcome})

    def _report_progress(self, job_id: str, token: str, pct: int, ctx: JobContext) -> None:
        with self.session_factory() as db:
            if not services.update_progress(db, job_id, token, pct, self.cfg):
                ctx.lease_lost.set()

    def _drop_unclaimable(self, db: Session, job_id: str) -> None:
        job = db.get(Job, job_id)
        if job is None:
            # Poison message: queue entry with no job behind it.
            self.queue.incr_stat("poison_dropped")
            log.error("dropped queue entry for unknown job", extra={"job_id": job_id})
        else:
            # Stale entry: job was cancelled, already claimed, or re-published by the reconciler.
            log.debug("skipped stale queue entry", extra={"job_id": job_id, "status": job.status})

    def _set_active(self, job_id: str, running: bool) -> None:
        with self._active_lock:
            if running:
                self._active[job_id] = threading.current_thread().name
            else:
                self._active.pop(job_id, None)

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        log.info("worker starting", extra={"worker_id": self.worker_id, "concurrency": self.concurrency})
        targets = [(f"slot-{i}", self._slot_loop) for i in range(self.concurrency)]
        targets += [("maintenance", self._maintenance_loop), ("registry", self._registry_loop)]
        for name, target in targets:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def request_stop(self) -> None:
        if not self._stop.is_set():
            log.info("shutdown requested; finishing in-flight jobs", extra={"active_jobs": list(self._active)})
            self._stop.set()

    def stop(self, timeout: float | None = None) -> None:
        """Graceful shutdown: stop pulling new jobs, wait for in-flight jobs to finish."""
        self.request_stop()
        for thread in self._threads:
            thread.join(timeout)
        try:
            self.queue.unregister_worker(self.worker_id)
        except Exception:
            log.warning("could not unregister worker", exc_info=True)
        log.info("worker stopped", extra={"worker_id": self.worker_id})

    def wait_for_stop(self, timeout: float) -> bool:
        return self._stop.wait(timeout)

    def _slot_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once(timeout=self.cfg.poll_timeout)
            except Exception:
                log.exception("worker slot error; backing off")
                self._stop.wait(1)

    def _maintenance_loop(self) -> None:
        interval = self.cfg.maintenance_interval
        while not self._stop.is_set():
            try:
                # All maintenance steps are safe to run concurrently; the lock just avoids
                # every worker doing the same scan every tick.
                if self.queue.acquire_lock("maintenance", self.worker_id, ttl=interval):
                    with self.session_factory() as db:
                        services.run_maintenance(db, self.queue, self.cfg)
            except Exception:
                log.exception("maintenance pass failed")
            self._stop.wait(interval)

    def _registry_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._active_lock:
                    active = list(self._active)
                self.queue.register_worker(
                    self.worker_id,
                    {"host": socket.gethostname(), "pid": os.getpid(),
                     "concurrency": self.concurrency, "active_jobs": active},
                    ttl=15,
                )
            except Exception:
                log.warning("worker registry update failed", exc_info=True)
            self._stop.wait(5)
