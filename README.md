# Job Queue Service

A background job queue: a REST API accepts jobs, a Redis priority queue dispatches them, separate
worker processes execute them, and PostgreSQL stores their state and results. A React dashboard
shows everything live.

Design decisions and trade-offs: **[DECISIONS.md](DECISIONS.md)**.

```
Dteam Tuvia/
├── README.md
├── DECISIONS.md
├── docker-compose.yml     postgres, redis, migrate, api, worker ×2, frontend (+ tests profile)
├── Dockerfile             multi-target: backend / test / frontend
├── App/
│   ├── backend/           Python 3.12: FastAPI API + worker (package `app`)
│   ├── frontend/          React 18 + Vite dashboard, served by nginx
│   └── db/init.sql        creates the test database
└── Tests/                 pytest suite (34 tests)
```

---

## 1. How to run the project

Requirements: Docker with Docker Compose v2.

```bash
docker compose up --build
```

| URL                          | What                              |
| ---------------------------- | --------------------------------- |
| http://localhost:3000        | React dashboard                   |
| http://localhost:8000/docs   | API with interactive Swagger docs |
| http://localhost:8000/health | Health check + queue statistics   |

This starts PostgreSQL, Redis, a one-shot `migrate` service that creates the schema, the API, and
**two worker containers** with 2 job slots each. Scale workers with
`docker compose up -d --scale worker=4`. Stop with `docker compose down`, or
`docker compose down -v` to also wipe the data.

## 2. How to run tests

**Inside Docker, against real PostgreSQL and Redis** (recommended):

```bash
docker compose --profile test run --rm --build tests
```

**Locally, with no services needed** (SQLite + in-memory fake Redis), from the project root:

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r App/backend/requirements-dev.txt
pytest Tests
```

Both run the same 34 tests:

| Requirement            | Test(s)                                                                                                                                                                                      |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Submission & retrieval | `test_submit_and_retrieve_job`, `test_list_jobs_with_filters`, `test_submission_validates_payload`                                                                                           |
| Completion flow        | `test_job_completion_flow`, `test_other_job_types_complete`, `test_batch_job_tracks_progress`                                                                                                |
| Failure & retry        | `test_failure_retries_with_exponential_backoff_then_fails_temporarily`, `test_transient_failure_then_success`, `test_poison_message_goes_straight_to_dead_letter`, `test_job_timeout_is_enforced` |
| Cancellation           | `test_cancel_pending_and_scheduled_jobs`, `test_cancel_wins_over_stale_queue_entry`                                                                                                          |
| Idempotency            | `test_idempotency_returns_existing_job`, `test_idempotency_key_expires_after_ttl`                                                                                                            |
| Priority ordering      | `test_priority_ordering`, `test_queue_score_orders_priority_then_fifo`                                                                                                                       |
| Scheduled jobs         | `test_scheduled_job_waits_until_due`                                                                                                                                                         |
| Duplicate pickup       | `test_job_can_only_be_claimed_once`, `test_concurrent_workers_process_each_job_exactly_once`                                                                                                 |
| Crash recovery         | `test_crashed_worker_job_is_recovered`, `test_crashed_worker_on_last_attempt_fails_without_dead_lettering`, `test_reconciler_republishes_jobs_lost_from_redis`                                               |
| Graceful shutdown      | `test_graceful_shutdown_finishes_current_job`                                                                                                                                                |
| Health                 | `test_health_reports_queue_statistics`                                                                                                                                                       |
| Rerun                  | `test_rerun_creates_a_copy_of_a_finished_job`                                                                                                                                                |
| Dev reset              | `test_dev_reset_wipes_everything_only_when_enabled`                                                                                                                                          |
| Dead letter queue      | `Tests/test_dead_letter.py`: corrupted jobs move to the DLQ table; temporary failures don't; requeue needs a fixed payload; unknown types can only be discarded; filter and purge; dev-only injection |

## 3. How to submit a test job

With the stack running:

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
        "type": "email",
        "payload": {"to": "user@example.com", "subject": "Welcome!"},
        "priority": 10,
        "idempotency_key": "welcome-user-42"
      }'
```

PowerShell equivalent:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/jobs -ContentType "application/json" `
  -Body '{"type":"email","payload":{"to":"user@example.com","subject":"Welcome!"},"priority":10}'
```

Response (`201 Created`; sending the same `idempotency_key` again returns the same job with
`200` and the header `Idempotent-Replayed: true`):

```json
{ "id": "9693cfc2-180e-4971-8e0b-55ee8c2a326d", "type": "email", "status": "pending",
  "priority": 10, "attempts": 0, "max_attempts": 3, "progress": 0, "result": null, ... }
```

Then check its status/result, a second or two later:

```bash
curl http://localhost:8000/jobs/<id>
# "status": "completed", "result": {"message_id": "<...@mail.mock>", "status": "sent", ...}
curl http://localhost:8000/jobs/<id>/logs      # submitted -> claimed -> completed
```

You can also submit jobs from the dashboard at http://localhost:3000. Its **Dev** button (shown under
the jobs list while the **Processing** stat is selected) opens a panel with **Fill**, which submits 10 demo jobs including a few that fail temporarily,
plus 3 jobs with corrupted data that end up in the dead letter queue, and **Clear**, which wipes
all data.

**Other job types**, payloads to try:

| type      | example payload                                            | behavior                                |
| --------- | ---------------------------------------------------------- | --------------------------------------- |
| `email`   | `{"to": "a@b.com", "subject": "Hi", "body": "..."}`        | 1–3 s, returns mock `message_id`        |
| `webhook` | `{"url": "https://example.com/hook", "failure_rate": 0.2}` | 1–2 s, 20% simulated failures (retried) |
| `webhook` | `{"url": "https://example.com/hook", "failure_rate": 0, "fail_attempts": 1}` | "temporarily unavailable": fails the first N attempts, then succeeds on retry |
| `report`  | `{"report_type": "sales", "format": "pdf"}`                | 3–5 s, returns mock `file_url`          |
| `batch`   | `{"items": [1,2,3,4,5,6,7,8,9,10], "item_delay_ms": 300}`  | live `progress` %, returns summary      |

Optional job fields: `priority` (-100…100, higher first; default 0), `max_attempts` (default 3),
`delay_seconds` **or** `scheduled_at` (ISO time) for future execution, `timeout_seconds`
(default 300), `idempotency_key`.

**Failure scenarios to try:**

```bash
# Temporarily unavailable webhook: attempt 1 fails, the retry 30 s later succeeds
curl -X POST http://localhost:8000/jobs -H "Content-Type: application/json"   -d '{"type":"webhook","payload":{"url":"https://example.com/hook","failure_rate":0,"fail_attempts":1}}'

# Always-failing webhook: runs now, retries after 30 s and 120 s, then "failed (temporarily)" 
curl -X POST http://localhost:8000/jobs -H "Content-Type: application/json" \
  -d '{"type":"webhook","payload":{"url":"https://example.com/hook","failure_rate":1}}'

docker compose kill -s SIGKILL worker; docker compose start worker   # crash: job recovered after 30 s lease
docker compose stop worker                                            # graceful: in-flight jobs finish first

# Corrupted data (dev endpoint, bypasses validation): moved to the dead letter queue on attempt 1
curl -X POST "http://localhost:8000/dev/corrupted-jobs?count=3"
curl http://localhost:8000/dead-letter
```

## 4. Architecture overview

```
                    ┌──────────────────────┐
 client / React ───▶│  API (FastAPI)       │──── 1. INSERT job ──────────────┐
                    └──────────┬───────────┘                                 ▼
                               │ 2. ZADD job id (score = priority, FIFO)  ┌─────────────┐
                               ▼                                          │ PostgreSQL  │
                    ┌──────────────────────┐                              │ jobs        │
                    │ Redis                │                              │ job_logs    │
                    │  ready  (sorted set) │                              │ idempotency │
                    │  workers (registry)  │                              │ _keys       │
                    └──────────┬───────────┘                              │ dead_letter │
                               │                                          │ _jobs       │
                               │                                          └──────▲──────┘
                               │ 3. BZPOPMIN (atomic)                            │
                               ▼                                                 │
                    ┌──────────────────────┐  4. claim (conditional UPDATE),     │
                    │ Worker processes     │     heartbeat, progress, result ────┘
                    │  N slot threads      │
                    │  + maintenance loop  │  scheduler · lease reaper · reconciler
                    └──────────────────────┘
```

- **API**: validates and stores the job in PostgreSQL first, then publishes the job ID to Redis.
  Provides submit, get, list (filter by status/type), cancel, retry, rerun, logs and health.
- **Queue (Redis)**: a sorted set of ready job IDs. The score encodes priority (higher first) and
  arrival order (FIFO within a priority). `BZPOPMIN` hands each entry to exactly one worker.
  Also holds a registry of live workers.
- **Workers**: separate processes. Each slot pops an ID, then **claims** the job with a
  conditional `UPDATE … WHERE status='pending'`, so a job can only ever be claimed once. While
  running, a heartbeat renews the job's 30-second **lease**. Failures are retried with
  exponential backoff (30 s, then 120 s). After 3 attempts the job is **failed (temporarily)**
  and can be retried. A job with **corrupted data** (unknown type, invalid payload) is never
  retried: it is moved to the **dead letter queue**, a separate table where it can be inspected,
  fixed and requeued, or discarded. On SIGTERM a worker finishes its current job before exiting.
- **Maintenance loop** (inside each worker, coordinated by a Redis lock):
    - promotes due **scheduled** jobs to the queue;
    - **reaps** jobs whose lease expired (crashed worker) and retries them;
    - **reconciles** pending jobs missing from Redis;
    - purges idempotency keys older than 24 h.
- **Database (PostgreSQL)**: the source of truth for job state, attempts, results, errors,
  progress and the per-job log, plus the dead letter queue (`dead_letter_jobs`). Redis can be
  rebuilt from it at any time.

Job lifecycle: `SCHEDULED → PENDING → PROCESSING → COMPLETED | FAILED`, plus `CANCELLED` (from
pending/scheduled), manual retry `FAILED → PENDING`, and `PROCESSING → dead letter queue` for
corrupted data (requeue brings it back as `PENDING`). Details: [DECISIONS.md](DECISIONS.md).

---

## Reference

### API endpoints

| Method | Path                  | Description                                                     |
| ------ | --------------------- | --------------------------------------------------------------- |
| POST   | `/jobs`               | Submit a job (201; 200 + `Idempotent-Replayed` on key reuse)    |
| GET    | `/jobs/{id}`          | Status, progress, result or error                               |
| GET    | `/jobs?status=&type=` | List with filters, `limit` / `offset` pagination                |
| GET    | `/jobs/{id}/logs`     | Per-job log (info / warning / error, with metadata)             |
| POST   | `/jobs/{id}/cancel`   | Cancel a pending or scheduled job (409 otherwise)               |
| POST   | `/jobs/{id}/retry`    | Retry a failed job (409 otherwise)                              |
| POST   | `/jobs/{id}/rerun`    | New copy of a completed/cancelled/failed job (201)              |
| GET    | `/health`             | DB/Redis status, queue stats, active workers                    |
| GET    | `/dead-letter?type=`  | Dead letter queue (jobs with corrupted data), paginated         |
| GET    | `/dead-letter/{id}`   | One dead letter: payload, error and full log history            |
| POST   | `/dead-letter/{id}/requeue` | Back to the jobs list as pending; body `{"payload": {...}}` fixes the data (409 if still invalid) |
| DELETE | `/dead-letter/{id}`   | Discard one dead letter (204)                                   |
| DELETE | `/dead-letter?type=`  | Discard all dead letters (optionally one type)                  |
| POST   | `/dev/reset`          | Delete all data; only when `DEV_ENDPOINTS=true` (403 otherwise) |
| POST   | `/dev/corrupted-jobs?count=` | Inject jobs with corrupted data, bypassing validation; dev only |

### Configuration

Environment variables (defaults in `App/backend/app/config.py`, overridden in `docker-compose.yml`):

| Variable                  | Default | Meaning                                                          |
| ------------------------- | ------- | ---------------------------------------------------------------- |
| `DEFAULT_MAX_ATTEMPTS`    | 3       | Attempts before permanent failure                                |
| `RETRY_BASE_DELAY`        | 30      | Seconds before attempt 2                                         |
| `RETRY_BACKOFF_FACTOR`    | 4       | Multiplier per attempt (30 s → 120 s)                            |
| `LEASE_SECONDS`           | 30      | Job lease; how fast a dead worker is detected                    |
| `HEARTBEAT_INTERVAL`      | 10      | Lease renewal interval                                           |
| `WORKER_CONCURRENCY`      | 2       | Job slots per worker process                                     |
| `DEFAULT_JOB_TIMEOUT`     | 300     | Per-job timeout unless `timeout_seconds` is set                  |
| `IDEMPOTENCY_TTL_HOURS`   | 24      | How long idempotency keys are kept                               |
| `RECONCILE_GRACE_SECONDS` | 30      | Pending jobs missing from Redis get re-queued                    |
| `SIM_TIME_SCALE`          | 1       | Speed multiplier for the mock jobs' sleeps                       |
| `DEV_ENDPOINTS`           | false   | Enables the `/dev/*` endpoints (on in docker-compose; local use only) |

### Code map

```
App/backend/app/
  api/main.py          FastAPI routes
  services.py          job state machine: submit, claim, complete, fail, cancel, retry, dead letters, maintenance
  queue.py             Redis ready queue, worker registry
  models.py            Job, JobLog, DeadLetterJob, IdempotencyKey tables
  schemas.py           request/response models + per-job-type payload validation
  worker/worker.py     worker loop, heartbeats, maintenance thread, graceful shutdown
  worker/handlers.py   the four mock job types + JobContext (timeouts, progress)
  logging_config.py    structured JSON logging with job context
  migrate.py           schema creation
App/frontend/src/      React dashboard (health stats, submit form, job table, job detail + logs, dead letter queue)
Tests/                 conftest.py (fixtures), test_api.py, test_processing.py, test_dead_letter.py
```
