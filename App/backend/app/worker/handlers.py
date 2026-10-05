"""Mock job implementations and the context they run in."""

import random
import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from app.schemas import BatchPayload, EmailPayload, ReportPayload, WebhookPayload


class JobError(Exception):
    """A failed attempt. Retryable unless it is a PermanentJobError."""

    retryable = True


class PermanentJobError(JobError):
    """Poison message: retrying cannot help (unknown type, invalid payload)."""

    retryable = False


class JobTimeoutError(JobError):
    pass


class LeaseLostError(Exception):
    """The job was reaped/reassigned while running; the result must be discarded."""


class JobContext:
    """Handed to every handler. `sleep` is cooperative: it enforces the job timeout and aborts
    if the worker lost its lease, so handlers never need to check either themselves."""

    def __init__(
        self,
        job_id: str,
        attempt: int,
        timeout_seconds: float,
        time_scale: float = 1.0,
        rng: random.Random | None = None,
        on_progress: Callable[[int], None] | None = None,
    ):
        self.job_id = job_id
        self.attempt = attempt
        self.timeout_seconds = timeout_seconds
        self.deadline = time.monotonic() + timeout_seconds
        self.time_scale = time_scale
        self.rng = rng or random.Random()
        self.lease_lost = threading.Event()
        self._on_progress = on_progress
        self._last_progress = -1

    def check(self) -> None:
        if self.lease_lost.is_set():
            raise LeaseLostError(self.job_id)
        if time.monotonic() >= self.deadline:
            raise JobTimeoutError(f"job exceeded its timeout of {self.timeout_seconds}s")

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds * self.time_scale
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.1))

    def progress(self, percent: int) -> None:
        if percent != self._last_progress and self._on_progress:
            self._last_progress = percent
            self._on_progress(percent)


def handle_email(ctx: JobContext, p: EmailPayload) -> dict[str, Any]:
    ctx.sleep(ctx.rng.uniform(1, 3))
    return {"message_id": f"<{uuid4()}@mail.mock>", "to": p.to, "subject": p.subject, "status": "sent"}


def handle_webhook(ctx: JobContext, p: WebhookPayload) -> dict[str, Any]:
    started = time.monotonic()
    ctx.sleep(ctx.rng.uniform(1, 2))
    if ctx.attempt <= p.fail_attempts:
        raise JobError(
            f"webhook {p.method} {p.url} failed: 503 Service Unavailable "
            f"(simulated outage: attempt {ctx.attempt} of the first {p.fail_attempts} that fail)"
        )
    if ctx.rng.random() < p.failure_rate:
        raise JobError(f"webhook {p.method} {p.url} failed: 503 Service Unavailable (simulated)")
    return {
        "url": p.url,
        "method": p.method,
        "status_code": 200,
        "response_time_ms": round((time.monotonic() - started) * 1000),
    }


def handle_report(ctx: JobContext, p: ReportPayload) -> dict[str, Any]:
    ctx.sleep(ctx.rng.uniform(3, 5))
    return {
        "report_type": p.report_type,
        "format": p.format,
        "file_url": f"https://storage.example.com/reports/{ctx.job_id}.{p.format}",
        "size_bytes": ctx.rng.randint(10_000, 5_000_000),
    }


def handle_batch(ctx: JobContext, p: BatchPayload) -> dict[str, Any]:
    total = len(p.items)
    fail = set(p.fail_items)
    failures = []
    started = time.monotonic()
    for index, item in enumerate(p.items):
        ctx.sleep(p.item_delay_ms / 1000)
        if index in fail:
            failures.append({"index": index, "item": item, "error": "simulated item failure"})
        ctx.progress(int((index + 1) * 100 / total))
    return {
        "total": total,
        "succeeded": total - len(failures),
        "failed": len(failures),
        "failures": failures,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


HANDLERS: dict[str, tuple[type[BaseModel], Callable[[JobContext, Any], dict[str, Any]]]] = {
    "email": (EmailPayload, handle_email),
    "webhook": (WebhookPayload, handle_webhook),
    "report": (ReportPayload, handle_report),
    "batch": (BatchPayload, handle_batch),
}


def execute(ctx: JobContext, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        model, handler = HANDLERS[job_type]
    except KeyError:
        raise PermanentJobError(f"unknown job type '{job_type}'") from None
    try:
        parsed = model.model_validate(payload)
    except ValidationError as exc:
        raise PermanentJobError(f"invalid payload: {exc.errors(include_url=False)}") from None
    return handler(ctx, parsed)
