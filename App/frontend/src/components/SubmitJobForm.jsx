import { useState } from "react";
import { api } from "../api.js";

const TEMPLATES = {
  email: { to: "user@example.com", subject: "Welcome!", body: "Thanks for signing up." },
  webhook: { url: "https://hooks.example.com/orders", method: "POST", body: { order_id: 42 }, failure_rate: 0.2 },
  report: { report_type: "monthly-sales", format: "pdf", params: { month: "2026-09" } },
  batch: { items: Array.from({ length: 20 }, (_, i) => `item-${i + 1}`), item_delay_ms: 300, fail_items: [7] },
};

export default function SubmitJobForm({ onSubmitted }) {
  const [type, setType] = useState("email");
  const [payload, setPayload] = useState(JSON.stringify(TEMPLATES.email, null, 2));
  const [priority, setPriority] = useState(0);
  const [delay, setDelay] = useState("");
  const [maxAttempts, setMaxAttempts] = useState(3);
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);

  const changeType = (next) => {
    setType(next);
    setPayload(JSON.stringify(TEMPLATES[next], null, 2));
  };

  const submit = async (event) => {
    event.preventDefault();
    let parsed;
    try {
      parsed = JSON.parse(payload);
    } catch {
      setMessage({ kind: "error", text: "Payload is not valid JSON" });
      return;
    }
    const job = { type, payload: parsed, priority: Number(priority), max_attempts: Number(maxAttempts) };
    if (delay) job.delay_seconds = Number(delay);
    if (idempotencyKey.trim()) job.idempotency_key = idempotencyKey.trim();

    setBusy(true);
    try {
      const { status, body } = await api.submitJob(job);
      setMessage(
        status === 200
          ? { kind: "info", text: `Idempotency key reused, returned existing job ${body.id.slice(0, 8)}` }
          : { kind: "ok", text: `Created job ${body.id.slice(0, 8)} (${body.status})` }
      );
      onSubmitted(body);
    } catch (err) {
      setMessage({ kind: "error", text: err.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form" onSubmit={submit}>
      <label>
        Type
        <div className="segmented">
          {Object.keys(TEMPLATES).map((t) => (
            <button type="button" key={t} className={t === type ? "active" : ""} onClick={() => changeType(t)}>
              {t}
            </button>
          ))}
        </div>
      </label>

      <label>
        Payload (JSON)
        <textarea rows={8} value={payload} onChange={(e) => setPayload(e.target.value)} spellCheck={false} />
      </label>

      <div className="row">
        <label>
          Priority
          <input type="number" min={-100} max={100} value={priority} onChange={(e) => setPriority(e.target.value)} />
        </label>
        <label>
          Max attempts
          <input type="number" min={1} max={10} value={maxAttempts} onChange={(e) => setMaxAttempts(e.target.value)} />
        </label>
        <label>
          Delay (s)
          <input type="number" min={0} placeholder="now" value={delay} onChange={(e) => setDelay(e.target.value)} />
        </label>
      </div>

      <label>
        Idempotency key
        <input placeholder="optional" value={idempotencyKey} onChange={(e) => setIdempotencyKey(e.target.value)} />
      </label>

      <button className="primary" disabled={busy}>
        {busy ? "Submitting…" : "Submit job"}
      </button>
      {message && <div className={`notice notice-${message.kind}`}>{message.text}</div>}
    </form>
  );
}
