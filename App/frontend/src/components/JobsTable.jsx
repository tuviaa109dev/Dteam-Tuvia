import { useEffect, useState } from "react";
import { api } from "../api.js";
import { usePolling } from "../usePolling.js";
import ConfirmDialog from "./ConfirmDialog.jsx";
import StatusBadge, { Countdown, ProgressBar, formatTime } from "./StatusBadge.jsx";

const STATUSES = ["", "scheduled", "pending", "processing", "completed", "failed", "cancelled"];
const TYPES = ["", "email", "webhook", "report", "batch"];
const PAGE = 25;
const COLUMNS = 9;

// Per-row action, and the matching bulk action shown when the table is filtered to that status.
const ACTIONS = {
  failed: { label: "Retry", bulkLabel: "Retry all", verb: "retry", fn: api.retryJob },
  cancelled: { label: "Rerun", bulkLabel: "Rerun all", verb: "rerun", fn: api.rerunJob },
  completed: { label: "Rerun", verb: "rerun", fn: api.rerunJob },
};

function SentDue({ job }) {
  switch (job.status) {
    case "completed":
      return <span title={new Date(job.completed_at).toLocaleString()}>{formatTime(job.completed_at)}</span>;
    case "scheduled":
    case "pending":
      // updated_at = when the job entered its current waiting state.
      return <Countdown from={job.updated_at} to={job.run_at} dueLabel="in queue" />;
    case "processing":
      return <span className="muted">sending…</span>;
    default:
      return "—";
  }
}

function RowAction({ job, onAction }) {
  const action = ACTIONS[job.status];
  if (!action) return "—"; // scheduled / pending / processing
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

/** Every job ID matching the current filters (all pages, not just the visible one). */
async function fetchAllIds(status, type) {
  const ids = [];
  for (let offset = 0; ; offset += 200) {
    const page = await api.listJobs({ status, type, limit: 200, offset });
    ids.push(...page.items.map((j) => j.id));
    if (!page.items.length || ids.length >= page.total) return ids;
  }
}

export default function JobsTable({ refreshKey, selectedId, onSelect, onChanged, status, onStatusChange }) {
  const [type, setType] = useState("");
  const [offset, setOffset] = useState(0);
  const [actionError, setActionError] = useState(null);
  const [bulkConfirm, setBulkConfirm] = useState(false);
  const [bulkStatus, setBulkStatus] = useState(null);

  useEffect(() => setOffset(0), [status, type]);

  const { data, error } = usePolling(
    () => api.listJobs({ status, type, limit: PAGE, offset }),
    2000,
    [status, type, offset, refreshKey]
  );
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const bulk = ACTIONS[status]?.bulkLabel ? ACTIONS[status] : null;

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

  const runBulk = async () => {
    setBulkConfirm(false);
    setActionError(null);
    const ids = await fetchAllIds(status, type);
    let done = 0;
    let skipped = 0;
    for (const id of ids) {
      setBulkStatus({ kind: "info", text: `${bulk.bulkLabel}: ${done + skipped + 1}/${ids.length}…` });
      try {
        await bulk.fn(id);
        done += 1;
      } catch {
        skipped += 1; // e.g. its status changed meanwhile
      }
    }
    const verbed = bulk.verb === "retry" ? "Retried" : "Reran";
    setBulkStatus({ kind: skipped ? "error" : "ok", text: `${verbed} ${done} job${done === 1 ? "" : "s"}${skipped ? `, ${skipped} skipped` : ""}.` });
    onChanged();
    setTimeout(() => setBulkStatus(null), 5000);
  };

  return (
    <>
      <div className="table-head">
        <h2>Jobs {data && <span className="muted">({total})</span>}</h2>
        <div className="filters">
          <select
            className={`status-select ${status ? `status-${status}` : ""}`}
            value={status}
            onChange={(e) => onStatusChange(e.target.value)}
          >
            {STATUSES.map((s) => (
              <option key={s} value={s} className={s ? `status-${s}` : "status-all"}>{s || "all statuses"}</option>
            ))}
          </select>
          <select value={type} onChange={(e) => setType(e.target.value)}>
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
              <th>Progress</th><th>Created</th><th>Sent / Due</th><th>Action</th>
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
                <td><SentDue job={job} /></td>
                <td><RowAction job={job} onAction={runAction} /></td>
              </tr>
            ))}
            {data && items.length === 0 && (
              <tr><td colSpan={COLUMNS} className="empty">
                {status || type ? "No jobs match this filter." : "No jobs yet. Submit one on the left."}
              </td></tr>
            )}
          </tbody>
          {bulk && total > 1 && (
            <tfoot>
              <tr>
                <td colSpan={COLUMNS - 1} className="bulk-status">
                  {bulkStatus && <span className={`notice notice-${bulkStatus.kind}`}>{bulkStatus.text}</span>}
                </td>
                <td>
                  <button className="primary bulk-action" disabled={bulkStatus?.kind === "info"} onClick={() => setBulkConfirm(true)}>
                    {bulk.bulkLabel}
                  </button>
                </td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>

      {total > PAGE && (
        <div className="pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
          <span className="muted">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
          <button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</button>
        </div>
      )}

      {bulkConfirm && bulk && (
        <ConfirmDialog
          title={`${bulk.bulkLabel}?`}
          message={
            bulk.verb === "retry"
              ? `Are you sure you want to retry all ${total} failed ${type ? `${type} ` : ""}job${total === 1 ? "" : "s"}? Each one goes back to pending with a fresh set of attempts.`
              : `Are you sure you want to rerun all ${total} cancelled ${type ? `${type} ` : ""}job${total === 1 ? "" : "s"}? A new copy of each one will be added to the list.`
          }
          confirmLabel={`Yes, ${bulk.verb} all`}
          onConfirm={runBulk}
          onCancel={() => setBulkConfirm(false)}
        />
      )}
    </>
  );
}
