// Backoffice API client. The acting recruiter's name travels in X-Recruiter
// so every action lands in the audit trail with an author.

const NAME_KEY = "bo_recruiter_name";

export function recruiterName() {
  return localStorage.getItem(NAME_KEY) || "";
}

export function setRecruiterName(name) {
  localStorage.setItem(NAME_KEY, (name || "").trim());
}

async function request(method, path, body) {
  const headers = { "X-Recruiter": encodeURIComponent(recruiterName() || "recruiter") };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let detail = data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    const error = new Error(typeof detail === "object" && detail ? detail.message : detail || res.statusText);
    error.status = res.status;
    error.detail = data.detail;
    throw error;
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body = {}) => request("POST", path, body),
  patch: (path, body = {}) => request("PATCH", path, body),
  put: (path, body = {}) => request("PUT", path, body),
  del: (path) => request("DELETE", path),
};

export function qs(params) {
  const out = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      if (value.length) out.set(key, value.join(","));
    } else out.set(key, value);
  }
  return out.toString();
}
