import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { usePolling } from "../usePolling.js";
import StatusBadge, { Countdown, ProgressBar, formatTime } from "./StatusBadge.jsx";

const AUTO_CLOSE_MS = 3000;

function Json({ value }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export default function JobDetail({ jobId, onClose, onChanged, onRerun, onOpenDeadLetter }) {
  const { data: fetched, error: loadError, refresh } = usePolling(() => api.getJob(jobId), 1000, [jobId]);
  const { data: logs } = usePolling(() => api.getLogs(jobId), 1500, [jobId]);
  const [actionError, setActionError] = useState(null);
  const [closing, setClosing] = useState(false);
  const lastSeen = useRef(null);

  // A worker found this job's data corrupted and moved it out of the jobs table.
  const movedToDeadLetter = Boolean(loadError?.includes("dead letter queue"));
  // Ignore a stale response from the previously selected job while the new one loads.
  const job = !movedToDeadLetter && fetched?.id === jobId ? fetched : null;

  // Auto-close when the job *transitions* to completed while the panel is open
  // (opening an already-completed job does not trigger it).
  useEffect(() => {
    if (!job) return undefined;
    const previous = lastSeen.current;
    lastSeen.current = { id: job.id, status: job.status };
    if (previous?.id === job.id && previous.status !== "completed" && job.status === "completed") {
      setClosing(true);
      const timer = setTimeout(onClose, AUTO_CLOSE_MS);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [job?.id, job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setClosing(false);
    setActionError(null);
  }, [jobId]);

  const act = async (fn, after) => {
    setActionError(null);
    try {
      const result = await fn(jobId);
      if (after) after(result);
      else await refresh();
      onChanged();
    } catch (err) {
      setActionError(err.message);
    }
  };

  return (
    <aside className="drawer">
      <div className="drawer-head">
        <div>
          <div className="muted mono">{jobId}</div>
          {job && (
            <h2>
              {job.type} <StatusBadge status={job.status} />
            </h2>
          )}
        </div>
        <button onClick={onClose} aria-label="Close">✕</button>
      </div>

      {closing && <div className="notice notice-ok closing">Job completed. Closing in 3 seconds…</div>}

      {movedToDeadLetter && (
        <div className="notice notice-dead closing">
          <p>This job's data is corrupted, so it was moved to the dead letter queue.</p>
          <button className="row-action row-action-danger" onClick={() => onOpenDeadLetter(jobId)}>
            Open in dead letter queue
          </button>
        </div>
      )}

      {job && (
        <>
          <div className="actions">
            {["pending", "scheduled"].includes(job.status) && (
              <button className="danger" onClick={() => act(api.cancelJob)}>Cancel job</button>
            )}
            {job.status === "failed" && (
              <button className="primary" onClick={() => act(api.retryJob)}>Retry job</button>
            )}
            {["completed", "cancelled"].includes(job.status) && (
              <button className="primary" onClick={() => act(api.rerunJob, onRerun)}>Rerun job</button>
            )}
          </div>
          {actionError && <div className="notice notice-error">{actionError}</div>}

          <dl className="facts">
            <dt>Priority</dt><dd>{job.priority}</dd>
            <dt>Attempts</dt><dd>{job.attempts} / {job.max_attempts}</dd>
            <dt>Progress</dt><dd><ProgressBar value={job.progress} /> {job.progress}%</dd>
            <dt>Timeout</dt><dd>{job.timeout_seconds}s</dd>
            <dt>Created</dt><dd>{formatTime(job.created_at)}</dd>
            <dt>Started</dt><dd>{formatTime(job.started_at)}</dd>
            <dt>Completed</dt><dd>{formatTime(job.completed_at)}</dd>
            {job.status === "scheduled" && (
              <>
                <dt>Runs at</dt>
                <dd className="runs-at">
                  {formatTime(job.run_at)}
                  {/* updated_at = when the job entered `scheduled` (submission or failed attempt) */}
                  <Countdown from={job.updated_at} to={job.run_at} />
                </dd>
              </>
            )}
            {job.worker_id && (<><dt>Worker</dt><dd className="mono">{job.worker_id}</dd></>)}
            {job.idempotency_key && (<><dt>Idempotency key</dt><dd className="mono">{job.idempotency_key}</dd></>)}
          </dl>

          {job.error && (
            <div className="notice notice-error">
              <strong>{job.error_type}</strong>: {job.error}
            </div>
          )}

          <h3>Payload</h3>
          <Json value={job.payload} />
          {job.result && (<><h3>Result</h3><Json value={job.result} /></>)}
        </>
      )}

      <h3>Log</h3>
      <ol className="logs">
        {(movedToDeadLetter ? [] : logs ?? []).map((entry) => (
          <li key={entry.id} className={`log-${entry.level}`}>
            <span className="muted">{formatTime(entry.created_at)}</span>
            <span className="log-level">{entry.level}</span>
            <span>{entry.message}</span>
            {entry.metadata?.error && <div className="log-meta">{entry.metadata.error}</div>}
          </li>
        ))}
      </ol>
    </aside>
  );
}
