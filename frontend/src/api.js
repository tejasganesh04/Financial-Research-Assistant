import { readEvents } from "./stream.js";
import { client } from "./session.js";

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
    return "The server took too long to respond. Check saved history before trying again.";
  if (error.code === "ERR_NETWORK")
    return "Could not reach the server. Check your connection and try again.";
  return error.message || "The request could not be completed.";
}

const enc = encodeURIComponent;
const data = (request) => request.then((r) => r.data);

export const api = {
  me: () => data(client.get("/auth/me")),
  changePassword: (current_password, new_password) =>
    data(client.post("/auth/password", { current_password, new_password })),
  sessions: () => data(client.get("/auth/sessions")).then((d) => d.sessions),
  endSession: (id) => client.delete(`/auth/sessions/${enc(id)}`),
  endOtherSessions: () => data(client.post("/auth/sessions/sign-out-others")),

  conversations: () =>
    client
      .get("/conversations", { params: { limit: 100 } })
      .then((r) => r.data.threads),
  conversation: (id) => data(client.get(`/conversations/${enc(id)}`)),
  result: (id) => data(client.get(`/conversations/${enc(id)}/result`)),
  deleteConversation: (id) => client.delete(`/conversations/${enc(id)}`),

  approvals: () => client.get("/approvals").then((r) => r.data.approvals),
  approval: (id) => data(client.get(`/approvals/${enc(id)}`)),
  decide: (id, decision, reason) =>
    data(client.post(`/approvals/${enc(id)}/${decision}`, { reason })),

  // Administration
  users: () => data(client.get("/admin/users")).then((d) => d.users),
  roles: () => data(client.get("/admin/roles")),
  createUser: (body) => data(client.post("/admin/users", body)),
  updateUser: (username, body) =>
    data(client.patch(`/admin/users/${enc(username)}`, body)),
  resetPassword: (username, password) =>
    data(
      client.post(`/admin/users/${enc(username)}/password`, {
        password: password || null,
      }),
    ),
  signOutEverywhere: (username) =>
    data(client.post(`/admin/users/${enc(username)}/sign-out`)),
  setActive: (username, active) =>
    data(
      client.post(
        `/admin/users/${enc(username)}/${active ? "activate" : "deactivate"}`,
      ),
    ),
  unlock: (username) =>
    data(client.post(`/admin/users/${enc(username)}/unlock`)),
  userSessions: (username) =>
    data(client.get(`/admin/users/${enc(username)}/sessions`)).then(
      (d) => d.sessions,
    ),
  endUserSession: (username, id) =>
    client.delete(`/admin/users/${enc(username)}/sessions/${enc(id)}`),
  activity: (params) =>
    data(client.get("/admin/activity", { params })).then((d) => d.events),
  health: (range, tz) =>
    data(client.get("/admin/health", { params: { range, tz } })),

  // MCP connection and consent
  mcpServer: () => data(client.get("/oauth/server")),
  oauthRequest: (id) => data(client.get(`/oauth/requests/${enc(id)}`)),
  oauthDecide: (id, decision) =>
    data(client.post(`/oauth/requests/${enc(id)}/${decision}`)),

  // One streaming POST per question; never retried after it starts, to avoid repeating paid work.
  async ask(id, message, onEvent, signal) {
    let response;
    try {
      response = await client.post(
        `/conversations/${enc(id)}/messages/stream`,
        { message },
        { adapter: "fetch", responseType: "stream", timeout: 0, signal },
      );
    } catch (error) {
      // Validation and access errors are JSON, but this request uses a stream adapter.
      if (error.response?.data?.getReader) {
        const detail = await new Response(error.response.data)
          .json()
          .catch(() => null);
        if (detail) error.response.data = detail;
      }
      throw error;
    }
    await readEvents(response.data, onEvent);
  },

  async download(file) {
    const response = await client.get(`/documents/${enc(file)}`, {
      responseType: "blob",
    });
    const url = URL.createObjectURL(response.data);
    const link = document.createElement("a");
    link.href = url;
    link.download = file;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  },
};
