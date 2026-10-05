// Display names for statuses. "dead_letter" is not a job status in the API: it is the dead
// letter queue, a separate table holding jobs whose data can never be processed.
export const STATUS_LABELS = {
  scheduled: "scheduled",
  pending: "pending",
  processing: "processing",
  completed: "completed",
  failed: "failed (temporarily)",
  cancelled: "cancelled",
  dead_letter: "dead letter (corrupted data)",
};

export const statusLabel = (status) => STATUS_LABELS[status] ?? status;

export const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1);
