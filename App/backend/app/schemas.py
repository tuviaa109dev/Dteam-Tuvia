from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

JobType = Literal["email", "webhook", "report", "batch"]
StatusName = Literal["scheduled", "pending", "processing", "completed", "failed", "cancelled"]


# ---- per-type payloads (validated on submit by the API and again by the worker) -------------


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailPayload(_Payload):
    to: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(min_length=1, max_length=255)
    body: str = ""


class WebhookPayload(_Payload):
    url: str = Field(pattern=r"^https?://\S+$")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    body: dict[str, Any] | None = None
    # Probability of a simulated failure. The spec's 20% is the default; tests pin it to 0 or 1.
    failure_rate: float = Field(0.2, ge=0, le=1)


class ReportPayload(_Payload):
    report_type: str = Field(min_length=1, max_length=64)
    format: Literal["pdf", "csv", "xlsx"] = "pdf"
    params: dict[str, Any] = Field(default_factory=dict)


class BatchPayload(_Payload):
    items: list[Any] = Field(min_length=1, max_length=1000)
    item_delay_ms: int = Field(200, ge=0, le=10_000)
    # Indexes of items that should fail; shows per-item failures in the summary.
    fail_items: list[int] = Field(default_factory=list)


PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    "email": EmailPayload,
    "webhook": WebhookPayload,
    "report": ReportPayload,
    "batch": BatchPayload,
}


# ---- API models ------------------------------------------------------------------------------


class JobCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "email",
                "payload": {"to": "user@example.com", "subject": "Welcome", "body": "Hi!"},
                "priority": 5,
                "idempotency_key": "welcome-email-user-42",
            }
        }
    )

    type: JobType
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(0, ge=-100, le=100, description="Higher runs first")
    max_attempts: int | None = Field(None, ge=1, le=10)
    timeout_seconds: int | None = Field(None, ge=1, le=3600)
    scheduled_at: datetime | None = Field(None, description="Run no earlier than this time")
    delay_seconds: int | None = Field(None, ge=0, le=30 * 24 * 3600, description="Alternative to scheduled_at")
    idempotency_key: str | None = Field(None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def _validate(self) -> "JobCreate":
        if self.scheduled_at is not None and self.delay_seconds is not None:
            raise ValueError("use either scheduled_at or delay_seconds, not both")
        if self.scheduled_at is not None and self.scheduled_at.tzinfo is None:
            self.scheduled_at = self.scheduled_at.replace(tzinfo=timezone.utc)
        try:
            self.payload = PAYLOAD_MODELS[self.type].model_validate(self.payload).model_dump()
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'payload'}: {e['msg']}" for e in exc.errors()
            )
            raise ValueError(f"invalid payload for '{self.type}' job: {problems}") from None
        return self


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    payload: dict[str, Any]
    status: StatusName
    priority: int
    attempts: int
    max_attempts: int
    timeout_seconds: int
    progress: int
    result: dict[str, Any] | None
    error: str | None
    error_type: str | None
    idempotency_key: str | None
    scheduled_at: datetime | None
    run_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    worker_id: str | None
    dead_lettered_at: datetime | None


class JobLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    level: str
    message: str
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    created_at: datetime


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


class QueueStats(BaseModel):
    ready: int
    scheduled: int
    processing: int
    dead_letter: int
    oldest_pending_age_seconds: float | None
    poison_messages_dropped: int


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    redis: bool
    queue: QueueStats | None = None
    jobs_by_status: dict[str, int] | None = None
    workers: list[dict[str, Any]] | None = None
