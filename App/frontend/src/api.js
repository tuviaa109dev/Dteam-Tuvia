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
  devReset: () => request("/dev/reset", { method: "POST" }).then((r) => r.body),
};
