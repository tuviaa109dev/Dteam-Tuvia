import { api } from "../api.js";
import { capitalize, statusLabel } from "../statusLabels.js";
import { usePolling } from "../usePolling.js";

/** Stat tiles that filter the jobs table. `""` means "all statuses". */
const STATUS_STATS = ["", "scheduled", "pending", "processing", "completed", "failed", "cancelled", "dead_letter"].map(
  (status) => ({ status, label: status ? capitalize(statusLabel(status)) : "Total jobs" })
);

function Stat({ label, value, status, active, onClick }) {
  const tone = status ? `stat-${status}` : "";
  const content = (
    <>
      <div className="stat-value">{value ?? "—"}</div>
      <div className="stat-label">{label}</div>
    </>
  );
  if (!onClick) return <div className="stat">{content}</div>;
  return (
    <button type="button" className={`stat clickable ${tone} ${active ? "active" : ""}`} aria-pressed={active} onClick={onClick}>
      {content}
    </button>
  );
}

export default function HealthPanel({ refreshKey, activeStatus, onSelectStatus }) {
  const { data, error } = usePolling(api.health, 2000, [refreshKey]);
  const q = data?.queue;
  const counts = data?.jobs_by_status;
  const total = counts ? Object.values(counts).reduce((sum, n) => sum + n, 0) : null;
  const workers = data?.workers ?? [];

  return (
    <section className="health">
      <div className="health-status">
        <span className={`dot ${data?.status === "ok" ? "ok" : "bad"}`} />
        <strong>{error ? "API unreachable" : data?.status === "ok" ? "Healthy" : "Degraded"}</strong>
        {data && (
          <span className="muted">
            DB {data.database ? "✓" : "✗"} · Redis {data.redis ? "✓" : "✗"} · {workers.length} worker
            {workers.length === 1 ? "" : "s"} ({workers.reduce((n, w) => n + (w.active_jobs?.length || 0), 0)} busy)
            {q && <> · {q.ready} ready in queue</>}
          </span>
        )}
      </div>
      <div className="stats">
        {STATUS_STATS.map(({ label, status }) => (
          <Stat
            key={label}
            label={label}
            status={status}
            value={status === "dead_letter" ? q?.dead_letter : status ? counts?.[status] : total}
            active={activeStatus === status}
            onClick={() => onSelectStatus(status)}
          />
        ))}
        <Stat
          label="Oldest pending"
          value={q?.oldest_pending_age_seconds != null ? `${Math.round(q.oldest_pending_age_seconds)}s` : "—"}
        />
      </div>
    </section>
  );
}
