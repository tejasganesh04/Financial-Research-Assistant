import { useEffect, useState } from "react";
import { api, errorMessage } from "./api.js";
import {
  Alert,
  Badge,
  EmptyState,
  Icon,
  PageHeader,
  Spinner,
  formatDateTime,
  roleName,
  timeAgo,
} from "./components.jsx";

const EVENTS = {
  login: ["Signed in", "success"],
  login_failed: ["Failed sign-in", "danger"],
  account_locked: ["Locked after failed sign-ins", "danger"],
  logout: ["Signed out", "neutral"],
  password_changed: ["Password changed", "info"],
  password_change_failed: ["Wrong current password", "warning"],
  signed_out_everywhere: ["Signed out everywhere", "info"],
  session_ended: ["Session ended", "neutral"],
  account_created: ["Account created", "info"],
  account_activated: ["Account reactivated", "info"],
  account_deactivated: ["Account deactivated", "warning"],
  account_unlocked: ["Account unlocked", "info"],
  profile_updated: ["Profile updated", "neutral"],
  refresh_token_reused: ["Session ended: reused sign-in token", "danger"],
  mcp_approved: ["Approved an MCP connection", "info"],
  mcp_connected: ["Connected an MCP client", "success"],
  mcp_disconnected: ["Disconnected an MCP client", "neutral"],
};

const REASONS = {
  unknown_user: "unknown username",
  password: "wrong password",
  locked: "account locked",
  deactivated: "account deactivated",
};

function describe(event) {
  const detail = event.detail || {};
  const bits = [];
  if (detail.reason && REASONS[detail.reason])
    bits.push(REASONS[detail.reason]);
  if (detail.attempted) bits.push(`as “${detail.attempted}”`);
  if (detail.client) bits.push(detail.client);
  if (detail.role) bits.push(roleName(detail.role));
  if (detail.client === "web") bits.pop();
  if (typeof detail.ended === "number")
    bits.push(`${detail.ended} session${detail.ended === 1 ? "" : "s"}`);
  return bits.join(" · ");
}

export default function AdminActivity() {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    setBusy(true);
    setError("");
    try {
      setEvents(await api.activity({ limit: 200 }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    load();
  }, []);

  return (
    <div className="page">
      <PageHeader
        title="Activity"
        description="Sign-ins, failed attempts, MCP connections and account changes, newest first. Kept for 180 days."
        actions={
          <button className="button secondary" onClick={load} disabled={busy}>
            <Icon name="refresh" size={14} />
            Refresh
          </button>
        }
      />
      <Alert>{error}</Alert>
      <section className="panel table-panel">
        {events === null ? (
          <div className="panel-loading">
            <Spinner /> Loading activity…
          </div>
        ) : events.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Event</th>
                  <th>Account</th>
                  <th>By</th>
                  <th>Details</th>
                  <th>IP address</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {events.map((event) => {
                  const [label, tone] = EVENTS[event.event] || [
                    event.event,
                    "neutral",
                  ];
                  return (
                    <tr key={event.id}>
                      <td>
                        <Badge tone={tone} dot>
                          {label}
                        </Badge>
                      </td>
                      <td>
                        {event.username || <span className="muted">—</span>}
                      </td>
                      <td className="muted">
                        {event.actor && event.actor !== event.username
                          ? event.actor
                          : "—"}
                      </td>
                      <td className="muted">{describe(event) || "—"}</td>
                      <td className="muted mono">{event.ip || "—"}</td>
                      <td className="muted" title={formatDateTime(event.at)}>
                        {timeAgo(event.at)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon="activity" title="No activity yet" />
        )}
      </section>
    </div>
  );
}
