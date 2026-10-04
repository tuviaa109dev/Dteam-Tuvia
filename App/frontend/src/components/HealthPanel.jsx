import { api } from "../api.js";
import { usePolling } from "../usePolling.js";

function Stat({ label, value, tone }) {
  return (
    <div className={`stat ${tone || ""}`}>
      <div className="stat-value">{value ?? "—"}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export default function HealthPanel({ refreshKey }) {
  const { data, error } = usePolling(api.health, 2000, [refreshKey]);
  const q = data?.queue;
  const s = data?.jobs_by_status;
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
          </span>
        )}
      </div>
      <div className="stats">
        <Stat label="Ready in queue" value={q?.ready} />
        <Stat label="Scheduled" value={q?.scheduled} />
        <Stat label="Processing" value={q?.processing} />
        <Stat label="Completed" value={s?.completed} tone="good" />
        <Stat label="Failed" value={s?.failed} tone="bad" />
        <Stat label="Cancelled" value={s?.cancelled} />
        <Stat label="Dead letter" value={q?.dead_letter} tone={q?.dead_letter ? "bad" : ""} />
        <Stat
          label="Oldest pending"
          value={q?.oldest_pending_age_seconds != null ? `${Math.round(q.oldest_pending_age_seconds)}s` : "—"}
        />
      </div>
    </section>
  );
}
