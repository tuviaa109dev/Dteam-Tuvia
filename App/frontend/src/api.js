const BASE = "/api";

async function request(path, options = {}) {
  const response = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok && !(path === "/health" && body)) {
    const detail = body?.detail;
    const message = Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : detail;
    throw new Error(message || `${response.status} ${response.statusText}`);
  }
  return { status: response.status, body };
}

export const api = {
  health: () => request("/health").then((r) => r.body),
  listJobs: ({ status, type, limit = 50, offset = 0 }) => {
    const params = new URLSearchParams({ limit, offset });
    if (status) params.set("status", status);
    if (type) params.set("type", type);
    return request(`/jobs?${params}`).then((r) => r.body);
  },
  getJob: (id) => request(`/jobs/${id}`).then((r) => r.body),
  getLogs: (id) => request(`/jobs/${id}/logs`).then((r) => r.body),
  submitJob: (job) => request("/jobs", { method: "POST", body: JSON.stringify(job) }),
  cancelJob: (id) => request(`/jobs/${id}/cancel`, { method: "POST" }).then((r) => r.body),
  retryJob: (id) => request(`/jobs/${id}/retry`, { method: "POST" }).then((r) => r.body),
  rerunJob: (id) => request(`/jobs/${id}/rerun`, { method: "POST" }).then((r) => r.body),

  // dead letter queue (jobs with corrupted data)
  listDeadLetters: ({ type, limit = 50, offset = 0 }) => {
    const params = new URLSearchParams({ limit, offset });
    if (type) params.set("type", type);
    return request(`/dead-letter?${params}`).then((r) => r.body);
  },
  getDeadLetter: (id) => request(`/dead-letter/${id}`).then((r) => r.body),
  requeueDeadLetter: (id, payload) =>
    request(`/dead-letter/${id}/requeue`, {
      method: "POST",
      body: JSON.stringify(payload === undefined ? {} : { payload }),
    }).then((r) => r.body),
  discardDeadLetter: (id) => request(`/dead-letter/${id}`, { method: "DELETE" }),
  purgeDeadLetters: (type) =>
    request(`/dead-letter${type ? `?type=${encodeURIComponent(type)}` : ""}`, { method: "DELETE" }).then((r) => r.body),

  // dev tools (DEV_ENDPOINTS=true)
  devReset: () => request("/dev/reset", { method: "POST" }).then((r) => r.body),
  devCorruptedJobs: (count = 3) => request(`/dev/corrupted-jobs?count=${count}`, { method: "POST" }).then((r) => r.body),
};
