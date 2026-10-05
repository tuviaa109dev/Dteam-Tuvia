import { useEffect, useState } from "react";
import { statusLabel } from "../statusLabels.js";

export default function StatusBadge({ status }) {
  return <span className={`badge badge-${status}`}>{statusLabel(status)}</span>;
}

export function ProgressBar({ value, tone }) {
  return (
    <div className={`progress ${tone ? `progress-${tone}` : ""}`} title={`${Math.round(value)}%`}>
      <div className="progress-fill" style={{ width: `${value}%` }} />
    </div>
  );
}

export function formatTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString();
}

function useNow(intervalMs) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

function formatRemaining(ms) {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Purple bar filling up from when the job was scheduled (`from`) until it is due (`to`). */
export function Countdown({ from, to, dueLabel = "due now" }) {
  const now = useNow(250);
  const start = new Date(from).getTime();
  const end = new Date(to).getTime();
  const span = Math.max(1, end - start);
  const pct = Math.min(100, Math.max(0, ((now - start) / span) * 100));
  const remaining = end - now;
  return (
    <span className="countdown">
      <ProgressBar value={pct} tone="scheduled" />
      <span className="muted">{remaining > 0 ? `in ${formatRemaining(remaining)}` : dueLabel}</span>
    </span>
  );
}
