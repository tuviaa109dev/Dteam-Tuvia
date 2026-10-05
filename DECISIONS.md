# Design Decisions

## 1. Job Pickup Strategy

**Approach chosen:** Two layers: an atomic pop from Redis, then a lease-based claim in PostgreSQL.

The ready queue is a Redis sorted set holding only job IDs. A worker takes the next ID with
`BZPOPMIN`, which is atomic, so one queue entry is delivered to exactly one worker. Before
running anything, the worker claims the job in the database with a compare-and-set update:

```sql
UPDATE jobs
SET status = 'processing', attempts = attempts + 1,
    worker_id = :worker, lease_token = :new_token, lease_expires_at = now() + 30s
WHERE id = :id AND status = 'pending' AND run_at <= now()
```

The worker only runs the job if exactly one row was updated. Each claim also gets a new
`lease_token`, which works as a fencing token. Every later write from that worker (heartbeat,
progress, complete, fail) must include the token, otherwise it is ignored.

**Why:** I wanted PostgreSQL to be the single source of truth and Redis to be only a fast
dispatch layer. The Redis pop alone prevents most duplicates. The conditional `UPDATE` is the real
guarantee: even if the same ID ends up in Redis twice (re-published after a failure, or popped
just after the job was cancelled), only one claim can match the row. The other worker sees
`rowcount = 0` and drops the entry. The fencing token covers the remaining edge case: a worker
we assumed was dead comes back and tries to write a result for a job that has already been
handed to someone else.

**Trade-offs:**

- _Gained:_ workers block on Redis instead of polling the database, so an idle system puts no
  load on Postgres. Priority ordering comes for free from the sorted set, and losing Redis data
  can never lose a job, because a reconciler re-publishes any pending job missing from the queue.
- _Gave up:_ simplicity. There are two systems to keep in sync, and the order of operations
  matters (commit to the DB first, then publish to Redis). Delivery is **at-least-once**, not
  exactly-once: if a worker dies mid-job, the job runs again, so real handlers would need to be
  idempotent downstream. A single-store design (`SELECT … FOR UPDATE SKIP LOCKED` polling) would
  have fewer moving parts, but it adds polling load and makes priority-plus-scheduling queries
  heavier as the table grows.

---

## 2. Worker Crash Recovery

**Approach chosen:** Leases with heartbeats, plus a background reaper.

- Claiming a job gives the worker a 30-second lease (`lease_expires_at`).
- While the job runs, a heartbeat thread extends the lease every 10 seconds. Progress updates
  from batch jobs extend it too.
- A maintenance loop runs every second inside the workers. A short Redis lock means usually only
  one worker runs it per tick, and it is safe if several do. It looks for
  `status = 'processing' AND lease_expires_at < now()` using `FOR UPDATE SKIP LOCKED`.

**Why:** A lease does not depend on the crashed process doing anything: if it stops
heart-beating, the lease simply runs out. This handles every kind of death the same way
(SIGKILL, OOM, a container being removed, a network partition). A fixed processing timeout alone
would either be too short for slow jobs or too slow to detect crashes. Heartbeats separate "the
job is slow" from "the worker is gone." I chose 30s/10s so a dead worker is noticed within about
half a minute, and three heartbeats have to be missed before a job is reclaimed.

**What happens if a worker crashes mid-job:**

1. The job stays in `processing`, and its heartbeats stop.
2. About 30 seconds later its lease expires.
3. The reaper locks the row and records the lost attempt as a failure
   (`error_type = LeaseExpired`). It clears the lease token and applies the normal retry policy:
    - attempts left → `scheduled` with backoff, and it runs again on a healthy worker;
    - no attempts left → `failed (temporarily)`. A crash says nothing about the job's data, so
      it is never sent to the dead letter queue; it can be retried once the cause is fixed.

    This stops a job that crashes its worker every time from looping forever.

4. If the "crashed" worker was only frozen and wakes up, its writes include the old lease token,
   which no longer matches. Its result is rejected and it cannot overwrite the new attempt.

Related safeguards that use the same loop:

- **Hung jobs:** the heartbeat stops renewing once a job passes its timeout, so the reaper picks
  up hung jobs too.
- **Missing queue entries:** a reconciler re-queues pending jobs that are missing from Redis, for
  example when a worker died between popping an ID and claiming it.
- **Graceful shutdown:** on SIGTERM a worker stops taking new jobs, finishes its current one,
  then exits.

---

## 3. Priority Queue Implementation

**Approach chosen:** A Redis sorted set where each member is a job ID and its score combines
priority and arrival order:

```
score = -priority × 10^12 + sequence      (sequence = Redis INCR counter)
```

`ZPOPMIN` / `BZPOPMIN` always returns the lowest score, which is the highest priority. Within the
same priority, the lowest sequence number comes first, so equal priorities run in FIFO order.
Priority is limited to −100…100, which keeps every score within the range a double represents
exactly (below 2^53). Re-publishing uses `ZADD NX`, so a job never loses its place in line.

**Why:** A sorted set gives O(log N) insert and pop, and the pop is atomic. Priority ordering
therefore costs nothing extra and needs no locking between workers. Putting the arrival
sequence into the score avoids starvation among same-priority jobs, which ordering by job ID or
using a priority-only score would not.

Scheduled jobs are deliberately kept **out** of this set until they are due. They wait in
PostgreSQL with status `scheduled` and a `run_at` time (indexed on `(status, run_at)`), and the
maintenance loop moves them into the queue when their time comes. The ready queue therefore only
ever contains jobs that can run right now, and the claim checks `run_at <= now()` again, so a
job can never start early.

---

## 4. Retry Backoff Strategy

**Approach chosen:** Exponential backoff, `delay = base × factor^(attempt − 1)`, with
`base = 30s` and `factor = 4`, both configurable through environment variables
(`RETRY_BASE_DELAY`, `RETRY_BACKOFF_FACTOR`). While it waits, a retrying job is `scheduled` with
`run_at` set to the retry time. Retries reuse the scheduled-job mechanism instead of adding a
separate delay queue. The last error stays on the job, and every attempt's error is kept in the
job log.

**Timing:**

| Attempt | When it runs                                                                 |
| ------- | ---------------------------------------------------------------------------- |
| 1       | Immediately                                                                  |
| 2       | 30 seconds after attempt 1 failed                                            |
| 3       | 2 minutes after attempt 2 failed                                             |
| —       | After attempt 3 fails → `failed (temporarily)`, stays in the list, can be retried |

Some rules on top of that:

- **Timeouts and crashes** count as normal failed attempts.
- **Manual retry** (`POST /jobs/{id}/retry`, or "Retry all" in the dashboard) resets a failed job
  to `pending` with a fresh attempt budget.
- **Corrupted data skips the retries and goes to the dead letter queue.** I separate two kinds of
  failure, because they need different responses:
    - **Failed (temporarily):** the data is fine, but something outside the job went wrong (a
      webhook was down, a worker crashed, a timeout). Retrying later can succeed, so the job stays
      in the jobs list as `failed`.
    - **Dead letter (corrupted data):** the job can *never* succeed as it is, for example an
      unknown job type or a payload that fails validation (the API validates on submit, so this
      means the data was damaged later or written by another producer). Retrying is pointless.
      On the first such error the worker moves the job, with its full log history, out of the
      `jobs` table into a separate `dead_letter_jobs` table, in one transaction.

  A separate table keeps the main jobs list free of jobs that will never run, and gives a
  developer a place to inspect them: `GET /dead-letter` lists them, and each one can be **requeued**
  with a corrected payload (refused with 409 while the payload is still invalid) or **discarded**.
  A requeued job goes back to `jobs` as `pending`, under its original ID and with its history.

---

## 5. One Thing I Would Do Differently With More Time

I would add **random jitter to the retry delays** and make the retry policy configurable **per job type**.

Right now every failed job waits exactly 30s and then exactly 120s. If a downstream service goes
down and 1,000 webhook jobs fail together, they all retry at the same second and hit the
recovering service as one spike (a "thundering herd"). Spreading each delay randomly (for example
±20%, or "full jitter" between 0 and the delay) would smooth that out. Different job types also
need different policies: a webhook to a flaky partner deserves more attempts and longer backoff
than a report job that fails for a deterministic reason. I would move `max_attempts`, the base
delay and the factor into a per-type configuration, and let handlers mark specific errors as
non-retryable (for example an HTTP 4xx from a webhook).

---

## 6. Why I Added the Front-End Even Though It Wasn't Defined in the Assignment

**Approach chosen:** A small React dashboard (`App/frontend`), served by nginx as its own container
in docker-compose. It talks to the backend only through the same public REST API a client would use.

**Why:**

- **Most of the interesting behavior happens over time.** The core of this system is a lifecycle:
  `scheduled → pending → processing → completed / failed`, plus retries with backoff, priority
  ordering and crash recovery. With `curl` you only see snapshots of it. In the dashboard I can
  watch it happen live: a batch job's progress bar filling, a webhook failing and being
  rescheduled 30 seconds later, a high-priority job overtaking a queue of low-priority ones, and
  a job being recovered after I kill a worker.
- **It made manual testing much faster.** The automated tests prove correctness. During
  development I still wanted to check behavior end to end against real PostgreSQL and Redis.
  The submit form, the status filters, the job log view and the "Fill" button (10 demo jobs,
  some designed to fail temporarily, plus 3 with corrupted data) let me do that in seconds
  instead of composing requests by hand.
- **It makes the project easier to review.** A reviewer can run `docker compose up` and
  immediately see the system working, without reading the API docs first.
- **It kept the API honest.** Building a real client on top of the API surfaced gaps and dead
  code. For example, it showed that the first version of the dead letter queue only duplicated
  the `failed` status (which led me to redesign it for corrupted data only), and that a "rerun"
  action for finished jobs was missing.

**Trade-offs:**

- *Gained:* faster feedback while developing, an easy demo, and a client that exercises the API
  the way a real consumer would.
- *Gave up:* time that could have gone into the backend, plus a second technology stack (Node/React)
  to build and maintain. I limited the cost by keeping the frontend fully separate. The backend
  doesn't depend on it, so the API, workers and tests all run without it.
- The only backend additions made purely for the dashboard are two dev endpoints:
  `POST /dev/reset` (the "Clear" button) and `POST /dev/corrupted-jobs` (the corrupted jobs that
  "Fill" injects, bypassing validation). Both are disabled by default and only enabled through
  `DEV_ENDPOINTS=true` in the local docker-compose setup.
