import { useEffect, useState } from "react";
import { sessionLabel } from "./AdminUsers.jsx";
import { api, errorMessage } from "./api.js";
import {
  Alert,
  Avatar,
  Badge,
  DOC_TYPES,
  Icon,
  PageHeader,
  Spinner,
  formatDateTime,
  roleName,
  timeAgo,
} from "./components.jsx";
import { adoptSession } from "./session.js";

export default function Account({ user }) {
  return (
    <div className="page narrow">
      <PageHeader
        title="Account"
        description="Your profile, password and signed-in devices."
      />
      <Profile user={user} />
      <ChangePassword />
      <Sessions />
    </div>
  );
}

function Profile({ user }) {
  const perms = user.permissions || {
    allowed_doc_types: [],
    allowed_confidentiality: [],
  };
  return (
    <section className="panel section-panel">
      <div className="profile-row">
        <Avatar name={user.name} size={48} />
        <div>
          <h2>{user.name}</h2>
          <span className="muted">
            {user.user_id}
            {user.department ? ` · ${user.department}` : ""}
          </span>
        </div>
        <Badge tone="info">{roleName(user.role)}</Badge>
      </div>
      <dl className="detail-grid two">
        <div>
          <dt>Documents you can search</dt>
          <dd>
            {perms.allowed_doc_types.includes("*")
              ? "All document types"
              : perms.allowed_doc_types
                  .map((t) => DOC_TYPES[t] || t)
                  .join(", ") || "None"}
          </dd>
        </div>
        <div>
          <dt>Confidentiality levels</dt>
          <dd>
            {perms.allowed_confidentiality.includes("*")
              ? "All levels"
              : perms.allowed_confidentiality
                  .map((level) => level[0].toUpperCase() + level.slice(1))
                  .join(", ") || "None"}
          </dd>
        </div>
      </dl>
      <p className="muted small">
        Your role is set by an administrator and applies in the app and in
        connected AI assistants.
      </p>
    </section>
  );
}

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const mismatch = confirm && next !== confirm;

  async function submit(event) {
    event.preventDefault();
    if (mismatch) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      adoptSession(await api.changePassword(current, next));
      setCurrent("");
      setNext("");
      setConfirm("");
      setNotice(
        "Password changed. Your other browsers and AI assistants have been signed out.",
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel section-panel">
      <div className="section-title">
        <h2>Password</h2>
        <p>
          Changing it signs you out everywhere else, including connected AI
          assistants.
        </p>
      </div>
      <form className="stack" onSubmit={submit}>
        <Alert>{error}</Alert>
        <Alert kind="success">{notice}</Alert>
        {/* Lets password managers associate the new password with this account. */}
        <input type="text" autoComplete="username" hidden readOnly />
        <div className="field">
          <label htmlFor="current-password">Current password</label>
          <input
            id="current-password"
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            required
            disabled={busy}
          />
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="new-password">New password</label>
            <input
              id="new-password"
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              required
              minLength={12}
              disabled={busy}
              aria-describedby="password-hint"
            />
          </div>
          <div className="field">
            <label htmlFor="confirm-password">Confirm new password</label>
            <input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
              disabled={busy}
              aria-invalid={mismatch || undefined}
            />
          </div>
        </div>
        <p
          id="password-hint"
          className={`muted small ${mismatch ? "error-text" : ""}`}
        >
          {mismatch
            ? "The new passwords do not match."
            : "At least 12 characters. A passphrase of a few words works well."}
        </p>
        <div>
          <button
            className="button primary"
            disabled={busy || !current || next.length < 12 || next !== confirm}
          >
            {busy && <Spinner />}
            Change password
          </button>
        </div>
      </form>
    </section>
  );
}

function Sessions() {
  const [sessions, setSessions] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  async function load() {
    try {
      setSessions(await api.sessions());
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function end(id) {
    setBusy(id);
    setError("");
    try {
      await api.endSession(id);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }

  async function endOthers() {
    setBusy("others");
    setError("");
    try {
      await api.endOtherSessions();
      await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }

  const others = (sessions || []).filter((s) => !s.current).length;

  return (
    <section className="panel section-panel">
      <div className="section-title">
        <h2>Sessions</h2>
        <p>
          Browsers you’re signed in on and AI assistants you’ve connected, such
          as Claude Code.
        </p>
      </div>
      <Alert>{error}</Alert>
      {sessions === null ? (
        <div className="panel-loading">
          <Spinner /> Loading…
        </div>
      ) : sessions.length ? (
        <ul className="session-list">
          {sessions.map((session) => (
            <li key={session.id}>
              <span className="session-icon">
                <Icon
                  name={session.kind === "mcp" ? "terminal" : "monitor"}
                  size={16}
                />
              </span>
              <div>
                <strong>
                  {sessionLabel(session)}
                  {session.current && (
                    <Badge tone="success">This browser</Badge>
                  )}
                </strong>
                <span>
                  Active {timeAgo(session.last_used_at).toLowerCase()} · since{" "}
                  {formatDateTime(session.created_at)}
                </span>
              </div>
              {!session.current && (
                <button
                  className="button ghost small"
                  onClick={() => end(session.id)}
                  disabled={!!busy}
                >
                  {busy === session.id && <Spinner />}
                  {session.kind === "mcp" ? "Disconnect" : "Sign out"}
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">No other sessions.</p>
      )}
      {others > 1 && (
        <div>
          <button
            className="button secondary"
            onClick={endOthers}
            disabled={!!busy}
          >
            {busy === "others" && <Spinner />}
            Sign out of all other sessions
          </button>
        </div>
      )}
    </section>
  );
}
