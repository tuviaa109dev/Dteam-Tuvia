import { useState } from "react";
import { api } from "../api.js";
import ConfirmDialog from "./ConfirmDialog.jsx";

const email = (to, subject) => ({ to, subject, body: "Demo message" });
const hook = (path, failure_rate, extra = {}) => ({
  url: `https://hooks.example.com/${path}`, method: "POST", failure_rate, ...extra,
});

// Submitted in this order. Mixes every job type, priorities and schedules, and three kinds of
// webhook failure:
// - "flaky-crm" is temporarily unavailable: its first attempt fails, the retry 30 s later succeeds
// - "orders" fails at random (20% per attempt), so it may or may not need a retry
// - "broken-partner" and "legacy-api" always fail and end up failed (temporarily)
// After these, Fill also injects CORRUPTED_COUNT jobs with corrupted data (see below).
export const CORRUPTED_COUNT = 3;

export const DEMO_JOBS = [
  { type: "email", payload: email("alice@example.com", "Welcome aboard") },
  { type: "report", payload: { report_type: "monthly-sales", format: "pdf", params: { month: "2026-09" } }, priority: 5 },
  { type: "batch", payload: { items: Array.from({ length: 20 }, (_, i) => `row-${i + 1}`), item_delay_ms: 300, fail_items: [7] } },
  { type: "webhook", payload: hook("broken-partner", 1), max_attempts: 1 },
  { type: "email", payload: email("ops@example.com", "URGENT: disk almost full"), priority: 50 },
  { type: "webhook", payload: hook("orders", 0.2) },
  { type: "webhook", payload: hook("flaky-crm", 0, { fail_attempts: 1 }) },
  { type: "report", payload: { report_type: "inventory", format: "csv" }, delay_seconds: 45 },
  { type: "email", payload: email("bob@example.com", "Your weekly digest"), priority: 10, delay_seconds: 20 },
  { type: "webhook", payload: hook("legacy-api", 1), max_attempts: 1, priority: 20 },
];

const DIALOGS = {
  fill: {
    title: "Add demo jobs?",
    message:
      `Are you sure you want to add ${DEMO_JOBS.length} demo jobs to the list? Some of them are designed to fail ` +
      `temporarily, plus ${CORRUPTED_COUNT} jobs with corrupted data that will end up in the dead letter queue.`,
    confirmLabel: "Yes, add jobs",
  },
  clear: {
    title: "Remove all jobs?",
    message:
      "Are you sure you want to remove all jobs from the list? This permanently deletes every job, log, dead letter and queued entry and starts over with a clean database.",
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
    const steps = DEMO_JOBS.length + 1;
    setBusy(`Submitting 0/${DEMO_JOBS.length}…`);
    let created = 0;
    try {
      for (const [index, job] of DEMO_JOBS.entries()) {
        await api.submitJob(job); // sequential, so the submission order is preserved
        created += 1;
        setBusy(`Submitting ${index + 1}/${DEMO_JOBS.length}…`);
      }
      // Corrupted data can't go through the API (it would be rejected with 422), so a dev
      // endpoint writes these straight to the database. Workers will dead-letter them.
      setBusy(`Injecting ${CORRUPTED_COUNT} corrupted jobs (${steps}/${steps})…`);
      await api.devCorruptedJobs(CORRUPTED_COUNT);
      setStatus({
        kind: "ok",
        text: `Added ${created} demo jobs and ${CORRUPTED_COUNT} jobs with corrupted data.`,
      });
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
      const { deleted_jobs, deleted_dead_letters } = await api.devReset();
      setStatus({
        kind: "ok",
        text: `Removed ${deleted_jobs} jobs and ${deleted_dead_letters} dead letters. Database is clean.`,
      });
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
            <strong>Fill</strong> submits {DEMO_JOBS.length} demo jobs (all types, priorities, schedules and some
            temporary failures) and injects {CORRUPTED_COUNT} jobs with corrupted data for the dead letter queue.{" "}
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
