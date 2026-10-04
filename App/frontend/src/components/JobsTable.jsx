import { useState } from "react";
import { api } from "../api.js";
import { usePolling } from "../usePolling.js";
import StatusBadge, { ProgressBar, formatTime } from "./StatusBadge.jsx";

const STATUSES = ["", "scheduled", "pending", "processing", "completed", "failed", "cancelled"];
const TYPES = ["", "email", "webhook", "report", "batch"];
const PAGE = 25;

function NextRun({ job, onAction }) {
  if (job.status === "scheduled") return formatTime(job.run_at);
  const action =
    job.status === "failed" ? { label: "Retry", fn: api.retryJob }
    : ["completed", "cancelled"].includes(job.status) ? { label: "Rerun", fn: api.rerunJob }
    : null; // pending / processing: about to run or running
  if (!action) return "—";
  return (
    <button
      className="row-action"
      onClick={(e) => {
        e.stopPropagation(); // don't also select the row
        onAction(action.fn, job);
      }}
    >
      {action.label}
    </button>
  );
}

export default function JobsTable({ refreshKey, selectedId, onSelect, onChanged }) {
  const [status, setStatus] = useState("");
  const [type, setType] = useState("");
  const [offset, setOffset] = useState(0);
  const [actionError, setActionError] = useState(null);

  const runAction = async (fn, job) => {
    setActionError(null);
    try {
      const result = await fn(job.id);
      onSelect(result.id); // the retried job, or the new copy for a rerun
      onChanged();
    } catch (err) {
      setActionError(err.message);
    }
  };

  const { data, error } = usePolling(
    () => api.listJobs({ status, type, limit: PAGE, offset }),
    2000,
    [status, type, offset, refreshKey]
  );
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <>
      <div className="table-head">
        <h2>Jobs {data && <span className="muted">({total})</span>}</h2>
        <div className="filters">
          <select value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
            {STATUSES.map((s) => <option key={s} value={s}>{s || "all statuses"}</option>)}
          </select>
          <select value={type} onChange={(e) => { setType(e.target.value); setOffset(0); }}>
            {TYPES.map((t) => <option key={t} value={t}>{t || "all types"}</option>)}
          </select>
        </div>
      </div>

      {(error || actionError) && <div className="notice notice-error">{error || actionError}</div>}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th><th>Type</th><th>Status</th><th>Priority</th><th>Attempts</th>
              <th>Progress</th><th>Created</th><th>Next run</th>
            </tr>
          </thead>
          <tbody>
            {items.map((job) => (
              <tr key={job.id} className={job.id === selectedId ? "selected" : ""} onClick={() => onSelect(job.id)}>
                <td className="mono">{job.id.slice(0, 8)}</td>
                <td>{job.type}</td>
                <td><StatusBadge status={job.status} /></td>
                <td className="num">{job.priority}</td>
                <td className="num">{job.attempts}/{job.max_attempts}</td>
                <td><ProgressBar value={job.progress} /></td>
                <td>{formatTime(job.created_at)}</td>
                <td><NextRun job={job} onAction={runAction} /></td>
              </tr>
            ))}
            {data && items.length === 0 && (
              <tr><td colSpan={8} className="empty">No jobs yet. Submit one on the left.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {total > PAGE && (
        <div className="pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
          <span className="muted">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
          <button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</button>
        </div>
      )}
    </>
  );
}
