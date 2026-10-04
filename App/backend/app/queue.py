import json
import time

from redis import Redis

# Score = -priority * PRIORITY_SPAN + sequence. ZPOPMIN therefore returns the highest priority
# first and, within one priority, the job enqueued first (FIFO). With priority in [-100, 100]
# the largest magnitude is ~1e14, well inside a double's exact integer range (2**53).
PRIORITY_SPAN = 10**12


class JobQueue:
    """Redis-backed dispatch queue.

    Redis holds only job IDs; PostgreSQL is the source of truth. Pops are atomic
    (ZPOPMIN/BZPOPMIN), so one queue entry is delivered to exactly one worker.
    """

    def __init__(self, client: Redis, prefix: str = "jobq"):
        self.redis = client
        self.prefix = prefix
        self.ready_key = f"{prefix}:ready"
        self.seq_key = f"{prefix}:seq"
        self.dlq_key = f"{prefix}:dlq"
        self.stats_key = f"{prefix}:stats"
        self.workers_prefix = f"{prefix}:workers:"

    @classmethod
    def from_url(cls, url: str, prefix: str = "jobq") -> "JobQueue":
        return cls(Redis.from_url(url, decode_responses=True, health_check_interval=30), prefix)

    @staticmethod
    def score(priority: int, seq: int) -> float:
        return -priority * PRIORITY_SPAN + seq

    def ping(self) -> bool:
        return bool(self.redis.ping())

    # ---- ready queue -------------------------------------------------------------------------

    def enqueue(self, job_id: str, priority: int) -> bool:
        """Add a job; NX keeps an existing entry's position. Returns True if newly added."""
        seq = self.redis.incr(self.seq_key)
        return bool(self.redis.zadd(self.ready_key, {job_id: self.score(priority, seq)}, nx=True))

    def pop(self, timeout: float = 0) -> tuple[str, float] | None:
        """Atomically remove the most urgent job ID. Blocks up to `timeout` seconds if > 0."""
        if timeout <= 0:
            items = self.redis.zpopmin(self.ready_key, 1)
            return (items[0][0], items[0][1]) if items else None
        item = self.redis.bzpopmin(self.ready_key, timeout=timeout)
        return (item[1], item[2]) if item else None

    def requeue(self, job_id: str, score: float) -> None:
        """Put a popped entry back at its original position (used during shutdown)."""
        self.redis.zadd(self.ready_key, {job_id: score}, nx=True)

    def remove(self, job_id: str) -> None:
        self.redis.zrem(self.ready_key, job_id)

    def contains(self, job_id: str) -> bool:
        return self.redis.zscore(self.ready_key, job_id) is not None

    def peek(self, limit: int = 100) -> list[str]:
        return self.redis.zrange(self.ready_key, 0, limit - 1)

    def depth(self) -> int:
        return int(self.redis.zcard(self.ready_key))

    # ---- dead letter queue -------------------------------------------------------------------

    def dead_letter(self, job_id: str) -> None:
        pipe = self.redis.pipeline()
        pipe.lrem(self.dlq_key, 0, job_id)
        pipe.lpush(self.dlq_key, job_id)
        pipe.execute()

    def remove_dead_letter(self, job_id: str) -> None:
        self.redis.lrem(self.dlq_key, 0, job_id)

    def dead_letters(self, limit: int = 100) -> list[str]:
        return self.redis.lrange(self.dlq_key, 0, limit - 1)

    def dlq_size(self) -> int:
        return int(self.redis.llen(self.dlq_key))

    def reset(self) -> None:
        """Drop all queued work, the DLQ and stats (dev reset). Live worker registrations stay."""
        self.redis.delete(self.ready_key, self.dlq_key, self.stats_key, self.seq_key)

    # ---- stats, worker registry, locks -------------------------------------------------------

    def incr_stat(self, name: str, amount: int = 1) -> None:
        self.redis.hincrby(self.stats_key, name, amount)

    def stats(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.redis.hgetall(self.stats_key).items()}

    def register_worker(self, worker_id: str, info: dict, ttl: float) -> None:
        payload = json.dumps({**info, "worker_id": worker_id, "seen_at": time.time()})
        self.redis.set(self.workers_prefix + worker_id, payload, px=int(ttl * 1000))

    def unregister_worker(self, worker_id: str) -> None:
        self.redis.delete(self.workers_prefix + worker_id)

    def active_workers(self) -> list[dict]:
        keys = list(self.redis.scan_iter(match=self.workers_prefix + "*", count=100))
        if not keys:
            return []
        return [json.loads(v) for v in self.redis.mget(keys) if v]

    def acquire_lock(self, name: str, owner: str, ttl: float) -> bool:
        return bool(self.redis.set(f"{self.prefix}:lock:{name}", owner, nx=True, px=int(ttl * 1000)))
