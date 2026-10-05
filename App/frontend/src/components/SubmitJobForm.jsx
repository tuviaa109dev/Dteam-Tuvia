import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const NOTICE_MS = 5000;

// Initial form values per job type. Edits are kept per type, so switching tabs doesn't lose them.
const DEFAULT_FIELDS = {
  email: { to: "user@example.com", subject: "Welcome!", body: "Thanks for signing up." },
  webhook: {
    url: "https://hooks.example.com/orders",
    method: "POST",
    body: [{ key: "order_id", value: "42" }],
    failure_rate: 0.2,
    fail_attempts: 0,
  },
  report: { report_type: "monthly-sales", format: "pdf", params: [{ key: "month", value: "2026-09" }] },
  batch: {
    items: Array.from({ length: 20 }, (_, i) => `item-${i + 1}`).join("\n"),
    item_delay_ms: 300,
    fail_items: "7",
  },
};

/** Key/value rows -> object. Values that parse as JSON (numbers, booleans) keep their type. */
function pairsToObject(pairs) {
  const entries = pairs
    .filter((p) => p.key.trim())
    .map((p) => {
      try {
        return [p.key.trim(), JSON.parse(p.value)];
      } catch {
        return [p.key.trim(), p.value];
      }
    });
  return Object.fromEntries(entries);
}

function buildPayload(type, f) {
  switch (type) {
    case "email":
      return { to: f.to, subject: f.subject, body: f.body };
    case "webhook": {
      const body = pairsToObject(f.body);
      return {
        url: f.url,
        method: f.method,
        ...(Object.keys(body).length ? { body } : {}),
        failure_rate: Number(f.failure_rate),
        ...(Number(f.fail_attempts) > 0 ? { fail_attempts: Number(f.fail_attempts) } : {}),
      };
    }
    case "report":
      return { report_type: f.report_type, format: f.format, params: pairsToObject(f.params) };
    case "batch":
      return {
        items: f.items.split("\n").map((s) => s.trim()).filter(Boolean),
        item_delay_ms: Number(f.item_delay_ms),
        fail_items: f.fail_items
          .split(",")
          .map((s) => parseInt(s.trim(), 10))
          .filter((n) => Number.isInteger(n) && n >= 0),
      };
    default:
      return {};
  }
}

function Field({ label, hint, children }) {
  return (
    <label>
      <span>
        {label}
        {hint && <span className="hint"> · {hint}</span>}
      </span>
      {children}
    </label>
  );
}

function KeyValueEditor({ label, pairs, onChange }) {
  const update = (index, patch) => onChange(pairs.map((p, i) => (i === index ? { ...p, ...patch } : p)));
  return (
    <div className="field">
      <span>{label}</span>
      {pairs.map((pair, index) => (
        <div className="kv-row" key={index}>
          <input placeholder="key" value={pair.key} onChange={(e) => update(index, { key: e.target.value })} />
          <input placeholder="value" value={pair.value} onChange={(e) => update(index, { value: e.target.value })} />
          <button type="button" aria-label="Remove" onClick={() => onChange(pairs.filter((_, i) => i !== index))}>
            ✕
          </button>
        </div>
      ))}
      <button type="button" className="link" onClick={() => onChange([...pairs, { key: "", value: "" }])}>
        + Add field
      </button>
    </div>
  );
}

function PayloadFields({ type, fields, set }) {
  switch (type) {
    case "email":
      return (
        <>
          <Field label="To">
            <input type="email" required value={fields.to} onChange={(e) => set({ to: e.target.value })} />
          </Field>
          <Field label="Subject">
            <input required maxLength={255} value={fields.subject} onChange={(e) => set({ subject: e.target.value })} />
          </Field>
          <Field label="Body">
            <textarea rows={3} value={fields.body} onChange={(e) => set({ body: e.target.value })} />
          </Field>
        </>
      );
    case "webhook":
      return (
        <>
          <div className="row row-url">
            <Field label="Method">
              <select value={fields.method} onChange={(e) => set({ method: e.target.value })}>
                {["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => <option key={m}>{m}</option>)}
              </select>
            </Field>
            <Field label="URL">
              <input type="url" required pattern="https?://.+" value={fields.url} onChange={(e) => set({ url: e.target.value })} />
            </Field>
          </div>
          <KeyValueEditor label="Request body" pairs={fields.body} onChange={(body) => set({ body })} />
          <Field label="Simulated failure rate" hint={`${Math.round(fields.failure_rate * 100)}%`}>
            <input
              type="range" min={0} max={1} step={0.05} value={fields.failure_rate}
              onChange={(e) => set({ failure_rate: Number(e.target.value) })}
            />
          </Field>
          <Field label="Unavailable for the first" hint="attempts, then recovers (simulated outage)">
            <input
              type="number" min={0} max={10} value={fields.fail_attempts}
              onChange={(e) => set({ fail_attempts: e.target.value })}
            />
          </Field>
        </>
      );
    case "report":
      return (
        <>
          <div className="row row-2">
            <Field label="Report type">
              <input required value={fields.report_type} onChange={(e) => set({ report_type: e.target.value })} />
            </Field>
            <Field label="Format">
              <select value={fields.format} onChange={(e) => set({ format: e.target.value })}>
                {["pdf", "csv", "xlsx"].map((f) => <option key={f}>{f}</option>)}
              </select>
            </Field>
          </div>
          <KeyValueEditor label="Parameters" pairs={fields.params} onChange={(params) => set({ params })} />
        </>
      );
    case "batch": {
      const count = fields.items.split("\n").filter((s) => s.trim()).length;
      return (
        <>
          <Field label="Items" hint={`one per line · ${count} item${count === 1 ? "" : "s"}`}>
            <textarea rows={5} required value={fields.items} onChange={(e) => set({ items: e.target.value })} />
          </Field>
          <div className="row row-2">
            <Field label="Delay per item (ms)">
              <input
                type="number" min={0} max={10000} value={fields.item_delay_ms}
                onChange={(e) => set({ item_delay_ms: e.target.value })}
              />
            </Field>
            <Field label="Items that fail" hint="indexes, comma-separated">
              <input placeholder="e.g. 2, 7" value={fields.fail_items} onChange={(e) => set({ fail_items: e.target.value })} />
            </Field>
          </div>
        </>
      );
    }
    default:
      return null;
  }
}

export default function SubmitJobForm({ onSubmitted }) {
  const [type, setType] = useState("email");
  const [fieldsByType, setFieldsByType] = useState(DEFAULT_FIELDS);
  const [priority, setPriority] = useState(0);
  const [delay, setDelay] = useState("");
  const [maxAttempts, setMaxAttempts] = useState(3);
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);

  const fields = fieldsByType[type];
  const setFields = (patch) => setFieldsByType((all) => ({ ...all, [type]: { ...all[type], ...patch } }));

  const request = useMemo(() => {
    const job = {
      type,
      payload: buildPayload(type, fields),
      priority: Number(priority),
      max_attempts: Number(maxAttempts),
    };
    if (delay !== "" && Number(delay) > 0) job.delay_seconds = Number(delay);
    if (idempotencyKey.trim()) job.idempotency_key = idempotencyKey.trim();
    return job;
  }, [type, fields, priority, maxAttempts, delay, idempotencyKey]);

  // Success/info notices float over the JSON preview for 5 s; errors stay until dismissed.
  useEffect(() => {
    if (!notice || notice.kind === "error") return undefined;
    const timer = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const { status, body } = await api.submitJob(request);
      setNotice({
        id: Date.now(),
        kind: status === 200 ? "info" : "ok",
        text:
          status === 200
            ? `Idempotency key reused: returned existing job ${body.id.slice(0, 8)}`
            : `Created job ${body.id.slice(0, 8)} (${body.status})`,
      });
      onSubmitted(body);
    } catch (err) {
      setNotice({ id: Date.now(), kind: "error", text: err.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form" onSubmit={submit}>
      <div className="field">
        <span>Type</span>
        <div className="segmented">
          {Object.keys(DEFAULT_FIELDS).map((t) => (
            <button type="button" key={t} className={t === type ? "active" : ""} onClick={() => setType(t)}>
              {t}
            </button>
          ))}
        </div>
      </div>

      <fieldset className="payload-fields">
        <legend>Payload</legend>
        <PayloadFields type={type} fields={fields} set={setFields} />
      </fieldset>

      <div className="row">
        <Field label="Priority">
          <input type="number" min={-100} max={100} value={priority} onChange={(e) => setPriority(e.target.value)} />
        </Field>
        <Field label="Max attempts">
          <input type="number" min={1} max={10} value={maxAttempts} onChange={(e) => setMaxAttempts(e.target.value)} />
        </Field>
        <Field label="Delay (s)">
          <input type="number" min={0} placeholder="now" value={delay} onChange={(e) => setDelay(e.target.value)} />
        </Field>
      </div>

      <Field label="Idempotency key">
        <input placeholder="optional" value={idempotencyKey} onChange={(e) => setIdempotencyKey(e.target.value)} />
      </Field>

      <button className="primary" disabled={busy}>
        {busy ? "Submitting…" : "Submit job"}
      </button>

      <div className="preview">
        <span className="preview-label">Request JSON</span>
        <textarea rows={10} disabled readOnly value={JSON.stringify(request, null, 2)} />
        {notice && (
          <div key={notice.id} className={`toast notice notice-${notice.kind}`} role="status">
            <span>{notice.text}</span>
            {notice.kind === "error" && (
              <button type="button" aria-label="Dismiss" onClick={() => setNotice(null)}>✕</button>
            )}
          </div>
        )}
      </div>
    </form>
  );
}
