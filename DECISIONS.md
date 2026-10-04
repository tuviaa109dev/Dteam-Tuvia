# Design decisions

## Architecture at a glance

```
client ─▶ API (FastAPI) ─▶ PostgreSQL (job state, source of truth)
                │
                └─▶ Redis ZSET "ready" (job IDs, ordered by priority) ─▶ worker processes ─▶ PostgreSQL
```

- **PostgreSQL is the source of truth** for every job's state, attempts, result, errors and logs.
- **Redis is the dispatch layer**: a sorted set of *job IDs* that are ready to run, plus a dead
  letter list, a worker registry and a maintenance lock. It never holds state that cannot be
  rebuilt from PostgreSQL.
- **Workers** are separate processes (`python -m app.worker`), each running N slot threads
  (`WORKER_CONCURRENCY`). Scale horizontally with `docker compose up --scale worker=N`.

Why both Redis and the DB instead of only `SELECT … FOR UPDATE SKIP LOCKED` polling: Redis gives
workers a blocking, push-style pop (`BZPOPMIN`) with no DB polling load when idle, and priority
ordering is a property of the data structure. Keeping the DB as the authority means Redis can
lose data (restart without AOF, eviction) and nothing is lost. The reconciler (below) re-publishes it.

## 1. Job pickup: preventing duplicates

Two independent layers:

1. **Atomic queue pop.** `BZPOPMIN` removes a member and returns it to exactly one client.
   Two workers can never receive the same queue entry.
2. **Compare-and-set claim in the DB.** Before running anything, the worker executes
   ```sql
   UPDATE jobs SET status='processing', attempts=attempts+1, worker_id=?, lease_token=?,
                   lease_expires_at=now()+lease
   WHERE id=? AND status='pending' AND run_at<=now()
   ```
   and only proceeds if exactly one row changed. This is the real guarantee: even if the same ID
   is in Redis twice (re-published by the reconciler, or a job was cancelled after it was
   popped), only one claim can succeed. A cancelled job's stale entry simply fails the claim and
   is dropped.

**Fencing token.** Each claim generates a fresh `lease_token`. Every later write by the worker
(heartbeat, progress, complete, fail) includes `WHERE lease_token = ?`. A worker that was
presumed dead and whose job was recovered can therefore never overwrite the new attempt's state
(tested in `test_crashed_worker_job_is_recovered`).

Delivery is **at-least-once**: a job whose worker dies mid-execution runs again. Real handlers
should be idempotent downstream (e.g. pass the job ID as an idempotency key to the email
provider). Exactly-once execution is not achievable across a crash without that cooperation.

## 2. Worker crash recovery

**Lease + heartbeat + reaper:**

- A claim grants a lease (`LEASE_SECONDS`, default 30s).
- While the job runs, a per-job heartbeat thread extends the lease every `HEARTBEAT_INTERVAL`
  (10s). Batch progress updates also extend it.
- A **reaper** (part of the maintenance loop) finds `status='processing' AND lease_expires_at < now`
  using `FOR UPDATE SKIP LOCKED` and treats each as a **failed attempt** (`error_type=LeaseExpired`).
  It follows the normal retry policy: rescheduled with backoff, or permanently failed and
  dead-lettered if attempts are exhausted. So a job that crashes its worker every time (a poison
  job) cannot loop forever.
- If the heartbeat finds its lease gone (`rowcount = 0`), it signals the handler to abort and the
  result is discarded.

A worker killed with SIGKILL is detected within ~lease duration (verified against the live
stack: job `processing` → `scheduled / LeaseExpired` after ~30s, then retried).

**Other gaps closed by the same loop:**

- *Reconciler*: `PENDING` jobs not touched for `RECONCILE_GRACE_SECONDS` are re-added to Redis
  with `ZADD NX` (no-op if already queued). This covers a failed enqueue after the DB commit, a
  worker dying between pop and claim, and Redis data loss.
- The API commits to PostgreSQL **before** publishing to Redis, so a worker can never pop an ID
  whose row doesn't exist yet. If the publish fails, the reconciler repairs it.

**Maintenance leadership:** each worker runs a maintenance thread, but a short Redis lock
(`SET NX PX`) means usually only one runs per tick. This is an optimisation, not a correctness
requirement: every step uses row locks with `SKIP LOCKED` or conditional updates, so concurrent
runs are safe and there is no single point of failure.

## 3. Retry with exponential backoff

`delay(n) = RETRY_BASE_DELAY × RETRY_BACKOFF_FACTOR^(n-1)`, defaults 30 × 4^(n-1):

| attempt | runs                                   |
|---------|----------------------------------------|
| 1       | immediately                            |
| 2       | 30 s after attempt 1 failed            |
| 3       | 120 s after attempt 2 failed           |
| —       | after attempt 3 fails: `FAILED` + DLQ  |

While waiting, the job is `SCHEDULED` with `run_at` = retry time. The retry reuses the scheduled
job mechanism rather than adding a separate delayed queue. The last error stays visible on the
job, and every attempt's error is in the job log.

**Non-retryable errors** (`PermanentJobError`: unknown job type, payload that fails validation in
the worker) skip the remaining attempts and go straight to `FAILED` + dead letter queue. Retrying
a poison message only wastes capacity.

**Timeouts** (`timeout_seconds` per job, default 300) are retryable failures (`JobTimeoutError`).
They are enforced cooperatively: handlers wait via `ctx.sleep()`, which checks the deadline. For
code that doesn't cooperate, the heartbeat stops renewing the lease once the deadline plus a grace
period has passed, so the reaper recovers the job anyway.

**Manual retry** (`POST /jobs/{id}/retry`) only accepts `FAILED` jobs. It resets attempts to 0,
clears error and result, removes the job from the DLQ and re-enqueues it.

## 4. Priority queue

Redis ZSET score = `-priority × 10^12 + seq`, where `seq` is a global `INCR` counter. `ZPOPMIN`
returns the highest priority first and is FIFO within a priority. Priority is limited to
[-100, 100], so scores stay within a double's exact integer range (2^53).
`ZADD NX` keeps a job's original queue position if it is re-published.

## 5. Scheduled jobs

`scheduled_at` (absolute) or `delay_seconds` (relative) creates the job as `SCHEDULED` with
`run_at` in the future. **It is not put in Redis.** The maintenance loop promotes due jobs
(`status='scheduled' AND run_at <= now`, indexed on `(status, run_at)`, `SKIP LOCKED`) to `PENDING`
and enqueues them. The claim also checks `run_at <= now`, so an early dispatch is impossible.
Promotion runs every `MAINTENANCE_INTERVAL` (1s), which is the scheduling precision.

Alternative considered: a second Redis ZSET keyed by timestamp. That would be a second copy of
state already in an indexed DB column, with its own consistency problems. The DB query is cheap.

## 6. Idempotency

A separate `idempotency_keys(key PRIMARY KEY, job_id, expires_at)` table:

- Submitting with a live key returns the existing job: HTTP **200** instead of 201, plus an
  `Idempotent-Replayed: true` header.
- Concurrent duplicate submissions race on the primary key. The loser gets an `IntegrityError`,
  rolls back and returns the winner's job, so there is never a duplicate.
- Keys live for `IDEMPOTENCY_TTL_HOURS` (24h) and are purged by the maintenance loop. An expired
  key can be reused for a new job. The job record itself is kept; only the key's dedupe window
  expires.
- A replay with a different payload returns the original job unchanged. This follows the usual
  semantics of "same key = same request" (Stripe-style). Returning 409 on a payload mismatch
  would be a reasonable stricter variant.

## Job lifecycle

```
SCHEDULED ──(run_at reached)──▶ PENDING ──(claim)──▶ PROCESSING ──▶ COMPLETED
    ▲  │                           │  ▲                  │
    │  └──────(cancel)──────┐      │  │                  ├──(retryable failure, attempts left)──▶ SCHEDULED (backoff)
    │                       ▼      │  │                  ├──(lease expired)─────────────────────▶ SCHEDULED / FAILED
    │                   CANCELLED ◀┘  │                  └──(attempts exhausted / permanent)────▶ FAILED ─▶ DLQ
    └──────────────────────────────── │ ◀──────────────────(manual retry)────────────────────────── FAILED
```

Cancellation is only allowed from `PENDING` / `SCHEDULED` (conditional UPDATE, otherwise 409).
Cancelling a running job is not supported, as specified.

## Graceful shutdown

On SIGTERM a worker stops pulling new jobs, lets each slot finish its current job, deregisters
and exits. A slot blocked in `BZPOPMIN` when shutdown begins puts any job it then receives back
at its original score. `stop_grace_period: 60s` in compose gives in-flight jobs time to finish.
After that Docker sends SIGKILL, and lease recovery takes over.

## Observability

- Structured JSON logs on stdout from the API and workers. A `ContextVar` automatically adds
  `job_id`, `job_type`, `attempt` and `worker_id` to every line logged while a job runs, including
  from the heartbeat thread.
- `job_logs` table: per-job audit trail (submitted, claimed, attempt failures with error and
  retry time, completion, cancellation, DLQ). Available at `GET /jobs/{id}/logs`.
- `GET /health`: DB and Redis status (503 if either is down), ready/scheduled/processing/DLQ
  counts, jobs by status, oldest pending age, poison messages dropped, and live workers with
  their active jobs (from a Redis registry with TTL).

## Dead letter queue

Permanently failed jobs get `dead_lettered_at` set in the DB and their ID pushed onto the Redis
list `jobq:dlq`. `GET /dead-letter` lists them, and a manual retry removes them. Queue entries
that point at no job at all (true poison messages) are dropped and counted in the
`poison_dropped` stat.

## Trade-offs and what I'd do next

- **Schema management** uses `create_all` in a one-shot `migrate` service. Production would use
  Alembic migrations.
- **Sync SQLAlchemy + threads** keeps handlers simple. For many I/O-bound jobs per worker, an
  asyncio worker would use fewer resources.
- **Tests** run on SQLite + fakeredis by default for speed, and the same suite runs against real
  PostgreSQL and Redis via `docker compose --profile test run --rm tests`. SQLite foreign keys
  are switched on so the two backends behave alike. That setting caught a real insert-ordering
  bug that only PostgreSQL had exposed.
- Not implemented: auth, rate limiting, job result retention/cleanup, cancellation of running
  jobs (would use the same `lease_lost` signal), metrics export (Prometheus).
