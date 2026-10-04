export default function StatusBadge({ status }) {
  return <span className={`badge badge-${status}`}>{status}</span>;
}

export function ProgressBar({ value }) {
  return (
    <div className="progress" title={`${value}%`}>
      <div className="progress-fill" style={{ width: `${value}%` }} />
    </div>
  );
}

export function formatTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString();
}
