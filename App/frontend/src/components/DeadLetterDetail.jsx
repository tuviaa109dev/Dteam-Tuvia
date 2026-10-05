import { useEffect, useState } from "react";
import { api } from "../api.js";
import ConfirmDialog from "./ConfirmDialog.jsx";
import StatusBadge, { formatTime } from "./StatusBadge.jsx";

/** Inspect a dead letter: see why it failed, edit its payload, then requeue it or discard it. */
export default function DeadLetterDetail({ jobId, onClose, onChanged, onRequeued }) {
  const [entry, setEntry] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [payloadText, setPayloadText] = useState("");
  const [actionError, setActionError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setEntry(null);
    setLoadError(null);
    setActionError(null);
    api.getDeadLetter(jobId).then(
      (data) => {
        if (cancelled) return;
        setEntry(data);
        setPayloadText(JSON.stringify(data.payload, null, 2));
      },
      (err) => !cancelled && setLoadError(err.message)
    );
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  const original = entry ? JSON.stringify(entry.payload, null, 2) : "";
  const edited = payloadText !== original;

  const requeue = async () => {
    setActionError(null);
    let payload;
    if (edited) {
      try {
        payload = JSON.parse(payloadText);
      } catch {
        setActionError("The payload is not valid JSON.");
        return;
      }
    }
    setBusy(true);
    try {
      const job = await api.requeueDeadLetter(jobId, payload);
      onChanged();
      onRequeued(job);
    } catch (err) {
      setActionError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const discard = async () => {
    setConfirmDiscard(false);
    try {
      await api.discardDeadLetter(jobId);
      onChanged();
      onClose();
    } catch (err) {
      setActionError(err.message);
    }
  };

  return (
    <aside className="drawer">
      <div className="drawer-head">
        <div>
          <div className="muted mono">{jobId}</div>
          {entry && (
            <h2>
              {entry.type} <StatusBadge status="dead_letter" />
            </h2>
          )}
        </div>
        <button onClick={onClose} aria-label="Close">✕</button>
      </div>

      {loadError && <div className="notice notice-error">{loadError}</div>}

      {entry && (
        <>
          <div className="notice notice-dead">
            <strong>{entry.error_type}</strong>: {entry.error}
          </div>

          <dl className="facts">
            <dt>Attempts</dt><dd>{entry.attempts} / {entry.max_attempts}</dd>
            <dt>Priority</dt><dd>{entry.priority}</dd>
            <dt>Created</dt><dd>{formatTime(entry.created_at)}</dd>
            <dt>Dead-lettered</dt><dd>{formatTime(entry.dead_lettered_at)}</dd>
            {entry.worker_id && (<><dt>Worker</dt><dd className="mono">{entry.worker_id}</dd></>)}
            {entry.idempotency_key && (<><dt>Idempotency key</dt><dd className="mono">{entry.idempotency_key}</dd></>)}
          </dl>

          <h3>Payload {edited && <span className="hint">· edited</span>}</h3>
          <textarea
            className="payload-editor"
            rows={8}
            spellCheck={false}
            value={payloadText}
            onChange={(e) => setPayloadText(e.target.value)}
          />
          <p className="muted dlq-help">
            Fix the data above, then requeue: the job goes back to the jobs list as pending, with its history.
            Requeueing is refused while the payload is still invalid for its type.
          </p>

          <div className="actions">
            <button className="primary" disabled={busy} onClick={requeue}>
              {busy ? "Requeueing…" : edited ? "Requeue with fixed payload" : "Requeue"}
            </button>
            {edited && (
              <button onClick={() => setPayloadText(original)} disabled={busy}>Undo edits</button>
            )}
            <button className="danger" onClick={() => setConfirmDiscard(true)} disabled={busy}>Discard</button>
          </div>
          {actionError && <div className="notice notice-error">{actionError}</div>}

          <h3>Log</h3>
          <ol className="logs">
            {entry.logs.map((item, index) => (
              <li key={index} className={`log-${item.level}`}>
                <span className="muted">{formatTime(item.created_at)}</span>
                <span className="log-level">{item.level}</span>
                <span>{item.message}</span>
                {item.metadata?.error && <div className="log-meta">{item.metadata.error}</div>}
              </li>
            ))}
          </ol>
        </>
      )}

      {confirmDiscard && (
        <ConfirmDialog
          title="Discard this dead letter?"
          message="Are you sure you want to permanently delete this job? Its data and logs cannot be recovered."
          confirmLabel="Yes, discard"
          danger
          onConfirm={discard}
          onCancel={() => setConfirmDiscard(false)}
        />
      )}
    </aside>
  );
}
