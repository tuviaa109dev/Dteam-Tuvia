import { useState } from "react";
import { api } from "../api.js";
import ConfirmDialog from "./ConfirmDialog.jsx";

const email = (to, subject) => ({ to, subject, body: "Demo message" });
const hook = (path, failure_rate) => ({ url: `https://hooks.example.com/${path}`, method: "POST", failure_rate });

// Submitted in this order. Mixes every job type, priorities and schedules, and includes
// webhooks with failure_rate 1 so some jobs end up FAILED (two right away, one after a 30 s retry).
export const DEMO_JOBS = [
  { type: "email", payload: email("alice@example.com", "Welcome aboard") },
  { type: "report", payload: { report_type: "monthly-sales", format: "pdf", params: { month: "2026-09" } }, priority: 5 },
  { type: "batch", payload: { items: Array.from({ length: 20 }, (_, i) => `row-${i + 1}`), item_delay_ms: 300, fail_items: [7] } },
  { type: "webhook", payload: hook("broken-partner", 1), max_attempts: 1 },
  { type: "email", payload: email("ops@example.com", "URGENT: disk almost full"), priority: 50 },
  { type: "webhook", payload: hook("orders", 0.2) },
  { type: "webhook", payload: hook("flaky-crm", 1), max_attempts: 2 },
  { type: "report", payload: { report_type: "inventory", format: "csv" }, delay_seconds: 45 },
  { type: "email", payload: email("bob@example.com", "Your weekly digest"), priority: 10, delay_seconds: 20 },
  { type: "webhook", payload: hook("legacy-api", 1), max_attempts: 1, priority: 20 },
];

const DIALOGS = {
  fill: {
    title: "Add demo jobs?",
    message: `Are you sure you want to add ${DEMO_JOBS.length} demo jobs to the list? Some of them are designed to fail.`,
    confirmLabel: "Yes, add jobs",
  },
  clear: {
    title: "Remove all jobs?",
    message:
      "Are you sure you want to remove all jobs from the list? This permanently deletes every job, log and queued entry and starts over with a clean database.",
    confirmLabel: "Yes, remove everything",
    danger: true,
  },
};

export default function DevPanel({ onFilled, onCleared }) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(null); // "fill" | "clear" awaiting confirmation
  const [busy, setBusy] = useState(null);
  const [status, setStatus] = useState(null);

  const fill = async () => {
    setBusy(`Submitting 0/${DEMO_JOBS.length}…`);
    let created = 0;
    try {
      for (const [index, job] of DEMO_JOBS.entries()) {
        await api.submitJob(job); // sequential, so the submission order is preserved
        created += 1;
        setBusy(`Submitting ${index + 1}/${DEMO_JOBS.length}…`);
      }
      setStatus({ kind: "ok", text: `Added ${created} demo jobs.` });
    } catch (err) {
      setStatus({ kind: "error", text: `Stopped after ${created} jobs: ${err.message}` });
    } finally {
      setBusy(null);
      onFilled();
    }
  };

  const clear = async () => {
    setBusy("Clearing…");
    try {
      const { deleted_jobs } = await api.devReset();
      setStatus({ kind: "ok", text: `Removed ${deleted_jobs} jobs. Database is clean.` });
      onCleared();
    } catch (err) {
      setStatus({ kind: "error", text: err.message });
    } finally {
      setBusy(null);
    }
  };

  const confirm = () => {
    const action = pending;
    setPending(null);
    setStatus(null);
    if (action === "fill") fill();
    if (action === "clear") clear();
  };

  return (
    <div className="dev">
      <button className={`dev-toggle ${open ? "active" : ""}`} onClick={() => setOpen(!open)} aria-expanded={open}>
        {"</>"} Dev
      </button>

      {open && (
        <section className="card dev-panel">
          <h2>Dev tools</h2>
          <div className="dev-actions">
            <button className="primary" disabled={!!busy} onClick={() => setPending("fill")}>
              Fill
            </button>
            <button className="danger" disabled={!!busy} onClick={() => setPending("clear")}>
              Clear
            </button>
          </div>
          <p className="muted dev-help">
            <strong>Fill</strong> submits {DEMO_JOBS.length} demo jobs (all types, priorities, schedules and some failures).{" "}
            <strong>Clear</strong> deletes all data.
          </p>
          {busy && <div className="notice notice-info">{busy}</div>}
          {!busy && status && <div className={`notice notice-${status.kind}`}>{status.text}</div>}
        </section>
      )}

      {pending && <ConfirmDialog {...DIALOGS[pending]} onConfirm={confirm} onCancel={() => setPending(null)} />}
    </div>
  );
}
