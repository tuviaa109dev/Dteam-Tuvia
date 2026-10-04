import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass
class Settings:
    """Runtime configuration, read from environment variables (see docker-compose.yml)."""

    database_url: str
    redis_url: str
    queue_prefix: str

    # Retry policy: attempt 1 runs immediately, attempt N+1 waits base * factor**(N-1).
    # Defaults give 30s before attempt 2 and 120s before attempt 3.
    default_max_attempts: int
    retry_base_delay: float
    retry_backoff_factor: float

    # Leases / crash recovery
    lease_seconds: float
    heartbeat_interval: float
    timeout_grace_seconds: float

    # Worker
    worker_concurrency: int
    poll_timeout: float
    maintenance_interval: float
    reconcile_grace_seconds: float

    default_job_timeout: int
    idempotency_ttl_hours: float
    # Multiplier applied to the mock jobs' simulated work (0 = instant, used by tests).
    sim_time_scale: float

    log_level: str
    cors_origins: list[str]
    # Enables POST /dev/reset (wipes all data). Never enable outside local development.
    dev_endpoints: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=_env("DATABASE_URL", "postgresql+psycopg://jobs:jobs@localhost:5432/jobs"),
            redis_url=_env("REDIS_URL", "redis://localhost:6379/0"),
            queue_prefix=_env("QUEUE_PREFIX", "jobq"),
            default_max_attempts=int(_env("DEFAULT_MAX_ATTEMPTS", "3")),
            retry_base_delay=float(_env("RETRY_BASE_DELAY", "30")),
            retry_backoff_factor=float(_env("RETRY_BACKOFF_FACTOR", "4")),
            lease_seconds=float(_env("LEASE_SECONDS", "30")),
            heartbeat_interval=float(_env("HEARTBEAT_INTERVAL", "10")),
            timeout_grace_seconds=float(_env("TIMEOUT_GRACE_SECONDS", "5")),
            worker_concurrency=int(_env("WORKER_CONCURRENCY", "2")),
            poll_timeout=float(_env("POLL_TIMEOUT", "1")),
            maintenance_interval=float(_env("MAINTENANCE_INTERVAL", "1")),
            reconcile_grace_seconds=float(_env("RECONCILE_GRACE_SECONDS", "30")),
            default_job_timeout=int(_env("DEFAULT_JOB_TIMEOUT", "300")),
            idempotency_ttl_hours=float(_env("IDEMPOTENCY_TTL_HOURS", "24")),
            sim_time_scale=float(_env("SIM_TIME_SCALE", "1")),
            log_level=_env("LOG_LEVEL", "INFO"),
            cors_origins=[o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()],
            dev_endpoints=_env("DEV_ENDPOINTS", "false").lower() in ("1", "true", "yes"),
        )

    def retry_delay(self, failed_attempt: int) -> float:
        """Seconds to wait before the attempt that follows `failed_attempt` (1-based)."""
        return self.retry_base_delay * self.retry_backoff_factor ** (failed_attempt - 1)


settings = Settings.from_env()
