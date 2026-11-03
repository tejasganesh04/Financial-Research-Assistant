import { readEvents } from "./stream.js";
import axios from "axios";

export const TOKEN_KEY = "financial-research-token";
// All these paths start with /v1. The browser and API share the same origin in the normal Docker setup.
const client = axios.create({ baseURL: "/v1", timeout: 20000 });

// Before each request, attach the JWT from this tab as Authorization: Bearer <token>. React user state is not sent.
client.interceptors.request.use((config) => {
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// A 401 from a protected endpoint expires the local session; a failed login stays on the login form.
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (
      error.response?.status === 401 &&
      !error.config.url.includes("/auth/login")
    ) {
      window.dispatchEvent(new Event("session-expired"));
    }
    return Promise.reject(error);
  },
);

export function errorMessage(error) {
  const detail = error.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item.type === "string_too_long" && item.loc?.includes("message"))
          return `Your message is too long. Please use ${item.ctx.max_length.toLocaleString()} characters or fewer.`;
        return item.msg;
      })
      .filter(Boolean)
      .join(" ");
  }
  if (error.code === "ECONNABORTED")
    return "The backend timed out. Check saved history before trying again.";
  if (error.code === "ERR_NETWORK")
    return "Could not connect to the backend. Check that the local services are running.";
  return error.message || "The request could not be completed.";
}

// HTTP helpers used by App.jsx: login -> identity -> sidebar/history -> question progress/result.
export const api = {
  login: (username, password) =>
    client.post("/auth/login", { username, password }).then((r) => r.data),
  me: () => client.get("/auth/me").then((r) => r.data),
  conversations: () =>
    client
      .get("/conversations", { params: { limit: 100 } })
      .then((r) => r.data.threads),
  conversation: (id) =>
    client.get(`/conversations/${encodeURIComponent(id)}`).then((r) => r.data),
  result: (id) =>
    client
      .get(`/conversations/${encodeURIComponent(id)}/result`)
      .then((r) => r.data),
  approvals: () => client.get("/approvals").then((r) => r.data.approvals),
  approval: (id) =>
    client.get(`/approvals/${encodeURIComponent(id)}`).then((r) => r.data),
  decide: (id, decision, reason) =>
    client
      .post(`/approvals/${encodeURIComponent(id)}/${decision}`, { reason })
      .then((r) => r.data),
  // Keep one POST open for SSE stage labels and the complete checked answer. Do not automatically retry paid work.
  async ask(id, message, onEvent, signal) {
    // SSE carries stage progress and one complete checked answer. No draft tokens.
    let response;
    try {
      response = await client.post(
        `/conversations/${encodeURIComponent(id)}/messages/stream`,
        { message },
        { adapter: "fetch", responseType: "stream", timeout: 0, signal },
      );
    } catch (error) {
      // Validation/access errors are JSON, but this request uses a stream adapter.
      // Decode their body so the existing error display can show the backend's explanation.
      if (error.response?.data?.getReader) {
        const detail = await new Response(error.response.data).json().catch(() => null);
        if (detail) error.response.data = detail;
      }
      throw error;
    }
    await readEvents(response.data, onEvent);
  },
  async download(file) {
    const response = await client.get(
      `/documents/${encodeURIComponent(file)}`,
      { responseType: "blob" },
    );
    const url = URL.createObjectURL(response.data);
    const link = document.createElement("a");
    link.href = url;
    link.download = file;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  },
};
