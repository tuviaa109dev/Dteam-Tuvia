from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Always stores UTC and always returns timezone-aware datetimes (also on SQLite)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


JSONType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class JobStatus(StrEnum):
    SCHEDULED = "scheduled"
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LogLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled','pending','processing','completed','failed','cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_jobs_progress"),
        # Scheduler: "scheduled jobs that are due"; reconciler: "pending jobs".
        Index("ix_jobs_status_run_at", "status", "run_at"),
        # Reaper: "processing jobs whose lease expired".
        Index("ix_jobs_status_lease", "status", "lease_expires_at"),
        Index("ix_jobs_type_status", "type", "status"),
        Index("ix_jobs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    error: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    # scheduled_at = what the client asked for; run_at = when the job is next eligible to run
    # (also moved forward by retry backoff).
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    idempotency_key: Mapped[str | None] = mapped_column(String(255), index=True)

    # Lease-based ownership. lease_token is a fencing token: every write from a worker must
    # present it, so a worker whose lease was reaped can no longer modify the job.
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    logs: Mapped[list["JobLog"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobLog.id"
    )


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, default=LogLevel.INFO)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="logs")


class DeadLetterJob(Base):
    """Dead letter queue: jobs whose data can never be processed (unknown type, invalid payload).

    A worker moves such a job here, out of `jobs`, in one transaction. The row is a snapshot of
    the job at that moment, including its full log history (as JSON, since its job_logs rows go
    with the original job). Jobs that merely ran out of retries are NOT moved here; they stay in
    `jobs` as `failed` and can be retried.
    """

    __tablename__ = "dead_letter_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # the original job's id
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    error_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    dead_lettered_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    logs: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)


class IdempotencyKey(Base):
    """Maps a client-supplied key to the job it created. The primary key on `key` is what
    makes concurrent duplicate submissions impossible; rows expire after the TTL (24h)."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
