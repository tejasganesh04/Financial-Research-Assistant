import { useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "./api.js";
import {
  Alert,
  EmptyState,
  Icon,
  PageHeader,
  Spinner,
  formatDateTime,
  roleName,
} from "./components.jsx";

// What each event is called, its icon, its tone and the filter it belongs to.
const EVENTS = {
  login: ["Signed in", "login", "success", "signin"],
  login_failed: ["Failed sign-in", "error", "danger", "signin"],
  account_locked: ["Locked after failed sign-ins", "lock", "danger", "signin"],
  logout: ["Signed out", "logout", "neutral", "signin"],
  password_changed: ["Password changed", "password", "info", "account"],
  password_change_failed: [
    "Wrong current password",
    "error",
    "warning",
    "account",
  ],
  signed_out_everywhere: ["Signed out everywhere", "logout", "info", "account"],
  session_ended: ["Session ended", "logout", "neutral", "signin"],
  account_created: ["Account created", "user", "info", "account"],
  account_activated: ["Account reactivated", "done", "info", "account"],
  account_deactivated: ["Account deactivated", "block", "warning", "account"],
  account_unlocked: ["Account unlocked", "key", "info", "account"],
  profile_updated: ["Profile updated", "edit", "neutral", "account"],
  refresh_token_reused: [
    "Session ended: reused sign-in token",
    "alert",
    "danger",
    "signin",
  ],
  mcp_approved: ["Approved an MCP connection", "plug", "info", "mcp"],
  mcp_connected: ["Connected an MCP client", "terminal", "success", "mcp"],
  mcp_disconnected: ["Disconnected an MCP client", "plug", "neutral", "mcp"],
  answer_approved: ["Released a reviewed answer", "done", "success", "review"],
  answer_rejected: ["Rejected a reviewed answer", "block", "warning", "review"],
};

const FILTERS = [
  ["all", "All"],
  ["signin", "Sign-ins"],
  ["problems", "Problems"],
  ["account", "Account changes"],
  ["mcp", "MCP clients"],
  ["review", "Reviews"],
];

const REASONS = {
  unknown_user: "unknown username",
  password: "wrong password",
  locked: "account locked",
  deactivated: "account deactivated",
};

const meta = (event) =>
  EVENTS[event.event] || [
    event.event.replaceAll("_", " "),
    "info",
    "neutral",
    "other",
  ];

// The secondary line: only what is known about the event, never placeholders.
function details(event) {
  const detail = event.detail || {};
  const bits = [];
  if (detail.reason && REASONS[detail.reason])
    bits.push(REASONS[detail.reason]);
  if (detail.attempted) bits.push(`tried “${detail.attempted}”`);
  if (detail.client && detail.client !== "web") bits.push(detail.client);
  if (detail.role) bits.push(roleName(detail.role));
  if (typeof detail.ended === "number")
    bits.push(`${detail.ended} session${detail.ended === 1 ? "" : "s"} ended`);
  if (event.actor && event.actor !== event.username)
    bits.push(`by ${event.actor}`);
  if (event.ip) bits.push(`from ${event.ip}`);
  return bits;
}

const dayFormat = new Intl.DateTimeFormat(undefined, {
  weekday: "long",
  day: "numeric",
  month: "long",
});
const timeFormat = new Intl.DateTimeFormat(undefined, { timeStyle: "short" });

function dayLabel(date) {
  const today = new Date();
  const start = (d) =>
    new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((start(today) - start(date)) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return dayFormat.format(date);
}

export default function AdminActivity() {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("all");
  const [account, setAccount] = useState("");

  async function load() {
    setBusy(true);
    setError("");
    try {
      setEvents(await api.activity({ limit: 300 }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    load();
  }, []);

  const accounts = useMemo(
    () =>
      [
        ...new Set((events || []).map((e) => e.username).filter(Boolean)),
      ].sort(),
    [events],
  );

  const days = useMemo(() => {
    const shown = (events || []).filter((event) => {
      const [, , tone, group] = meta(event);
      if (account && event.username !== account) return false;
      if (filter === "all") return true;
      if (filter === "problems") return tone === "danger" || tone === "warning";
      return group === filter;
    });
    const groups = [];
    for (const event of shown) {
      const label = dayLabel(new Date(event.at));
      const last = groups[groups.length - 1];
      if (last?.label === label) last.events.push(event);
      else groups.push({ label, events: [event] });
    }
    return groups;
  }, [events, filter, account]);

  return (
    <div className="page narrow">
      <PageHeader
        title="Activity"
        description="Sign-ins, failed attempts, MCP connections and account changes, newest first. Kept for 180 days."
      />
      <Alert>{error}</Alert>
      <div className="activity-filters">
        <div className="filter-chips" role="group" aria-label="Show">
          {FILTERS.map(([value, label]) => (
            <button
              key={value}
              className="filter-chip"
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {filter === value && <Icon name="check" size={14} />}
              {label}
            </button>
          ))}
        </div>
        <div className="activity-tools">
          {accounts.length > 1 && (
            <div className="field compact-field">
              <label htmlFor="activity-account">Account</label>
              <select
                id="activity-account"
                value={account}
                onChange={(e) => setAccount(e.target.value)}
              >
                <option value="">Everyone</option>
                {accounts.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </div>
          )}
          <button
            className="icon-button"
            onClick={load}
            disabled={busy}
            aria-label="Refresh activity"
            title="Refresh"
          >
            {busy ? <Spinner size={16} /> : <Icon name="refresh" size={16} />}
          </button>
        </div>
      </div>
      {events === null ? (
        <section className="panel">
          <div className="panel-loading">
            <Spinner /> Loading activity…
          </div>
        </section>
      ) : days.length ? (
        days.map((day) => (
          <section className="activity-day" key={day.label}>
            <h2 className="activity-day-label">{day.label}</h2>
            <ul className="panel activity-list">
              {day.events.map((event) => {
                const [label, icon, tone] = meta(event);
                const more = details(event);
                return (
                  <li key={event.id} className="activity-item">
                    <span className={`activity-icon ${tone}`}>
                      <Icon name={icon} size={16} />
                    </span>
                    <div className="activity-text">
                      <strong>
                        {label}
                        {event.username && (
                          <span className="activity-account">
                            {event.username}
                          </span>
                        )}
                      </strong>
                      {more.length > 0 && <span>{more.join(" · ")}</span>}
                    </div>
                    <time
                      dateTime={event.at}
                      title={formatDateTime(event.at)}
                      className="activity-time"
                    >
                      {timeFormat.format(new Date(event.at))}
                    </time>
                  </li>
                );
              })}
            </ul>
          </section>
        ))
      ) : (
        <section className="panel">
          <EmptyState
            icon="activity"
            title={events.length ? "Nothing matches" : "No activity yet"}
          >
            {events.length ? "Try another filter or account." : null}
          </EmptyState>
        </section>
      )}
    </div>
  );
}
