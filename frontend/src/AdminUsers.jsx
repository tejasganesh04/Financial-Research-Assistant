import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api.js";
import {
  Alert,
  Avatar,
  Badge,
  CopyField,
  EmptyState,
  Icon,
  Modal,
  PageHeader,
  ROLE_NAMES,
  Spinner,
  formatDateTime,
  roleName,
  timeAgo,
} from "./components.jsx";

function status(user) {
  if (!user.active) return { label: "Deactivated", tone: "neutral" };
  if (user.locked_until) return { label: "Locked", tone: "danger" };
  return { label: "Active", tone: "success" };
}

function sessionSummary(user) {
  const parts = [];
  if (user.web_sessions)
    parts.push(
      `${user.web_sessions} ${user.web_sessions === 1 ? "browser" : "browsers"}`,
    );
  if (user.mcp_sessions)
    parts.push(
      `${user.mcp_sessions} MCP ${user.mcp_sessions === 1 ? "client" : "clients"}`,
    );
  return parts.join(" · ") || "—";
}

export function sessionLabel(session) {
  if (session.kind === "mcp") return session.client_name || "MCP client";
  const ua = session.user_agent || "";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Chrome\//.test(ua)
      ? "Chrome"
      : /Firefox\//.test(ua)
        ? "Firefox"
        : /Safari\//.test(ua)
          ? "Safari"
          : "Browser";
  const os = /Mac OS X/.test(ua)
    ? "macOS"
    : /Windows/.test(ua)
      ? "Windows"
      : /iPhone|iPad/.test(ua)
        ? "iOS"
        : /Android/.test(ua)
          ? "Android"
          : /Linux/.test(ua)
            ? "Linux"
            : "";
  return os ? `${browser} on ${os}` : browser;
}

export default function AdminUsers({ me }) {
  const [users, setUsers] = useState(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [adding, setAdding] = useState(false);
  const [credentials, setCredentials] = useState(null);

  async function load() {
    try {
      const list = await api.users();
      setUsers(list);
      setSelected((current) =>
        current
          ? list.find((u) => u.username === current.username) || null
          : null,
      );
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  useEffect(() => {
    load();
  }, []);

  return (
    <div className="page">
      <PageHeader
        title="Users"
        description="Everyone who can sign in to the web app and connect an MCP client. There is no public sign-up."
        actions={
          <button className="button primary" onClick={() => setAdding(true)}>
            <Icon name="plus" size={15} />
            Add user
          </button>
        }
      />
      <Alert>{error}</Alert>
      <section className="panel table-panel">
        {users === null ? (
          <div className="panel-loading">
            <Spinner /> Loading users…
          </div>
        ) : users.length ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>User</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Last active</th>
                  <th>Signed in on</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {users.map((user) => {
                  const s = status(user);
                  return (
                    <tr
                      key={user.username}
                      onClick={() => setSelected(user)}
                      className="clickable"
                    >
                      <td>
                        <div className="user-cell">
                          <Avatar name={user.name} size={30} />
                          <div>
                            <strong>
                              {user.name}
                              {user.username === me.user_id && (
                                <span className="you">You</span>
                              )}
                            </strong>
                            <span>{user.username}</span>
                          </div>
                        </div>
                      </td>
                      <td>{user.role_label || roleName(user.role)}</td>
                      <td>
                        <Badge tone={s.tone} dot>
                          {s.label}
                        </Badge>
                      </td>
                      <td className="muted">
                        {timeAgo(user.last_seen_at || user.last_login_at)}
                      </td>
                      <td className="muted">{sessionSummary(user)}</td>
                      <td className="row-action">
                        <button
                          className="button ghost small"
                          onClick={(e) => {
                            e.stopPropagation();
                            setSelected(user);
                          }}
                          aria-label={`Manage ${user.username}`}
                        >
                          Manage
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon="users" title="No users yet">
            Add the first account, or run <code>make users</code> on the server.
          </EmptyState>
        )}
      </section>
      <p className="page-note">
        <Icon name="info" size={14} />
        Password resets, sign-outs and deactivations end a person’s sessions
        right away. A browser or MCP client that already holds an access token
        keeps it until it expires, at most one hour.
      </p>

      {selected && (
        <UserPanel
          user={selected}
          me={me}
          onClose={() => setSelected(null)}
          onChanged={load}
          onCredentials={setCredentials}
        />
      )}
      {adding && (
        <AddUser
          onClose={() => setAdding(false)}
          onCreated={(result) => {
            setAdding(false);
            load();
            if (result.password)
              setCredentials({
                username: result.user.username,
                password: result.password,
                created: true,
              });
          }}
        />
      )}
      {credentials && (
        <Credentials {...credentials} onClose={() => setCredentials(null)} />
      )}
    </div>
  );
}

function UserPanel({ user, me, onClose, onChanged, onCredentials }) {
  const [sessions, setSessions] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirm, setConfirm] = useState(null);
  const [resetting, setResetting] = useState(false);
  const [editing, setEditing] = useState(false);
  const isMe = user.username === me.user_id;
  const s = status(user);

  async function loadSessions() {
    try {
      setSessions(await api.userSessions(user.username));
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  useEffect(() => {
    setSessions(null);
    setNotice("");
    setError("");
    loadSessions();
  }, [user.username]);

  const drawer = useRef(null);
  const dialogOpen = !!confirm || resetting || editing;
  useEffect(() => {
    const previous = document.activeElement;
    drawer.current?.focus();
    return () => previous?.focus?.();
  }, []);
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape" && !dialogOpen) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dialogOpen]);

  async function run(label, action, message) {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice(message);
      await Promise.all([onChanged(), loadSessions()]);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
      setConfirm(null);
    }
  }

  const actions = {
    signOut: {
      title: `Sign ${user.name} out everywhere?`,
      body: "Every browser and MCP client they use is signed out. Their password stays the same.",
      button: "Sign out everywhere",
      run: () =>
        run(
          "signOut",
          () => api.signOutEverywhere(user.username),
          `${user.name} has been signed out everywhere.`,
        ),
    },
    deactivate: {
      title: `Deactivate ${user.name}?`,
      body: "They are signed out everywhere and cannot sign in until you reactivate the account.",
      button: "Deactivate",
      danger: true,
      run: () =>
        run(
          "deactivate",
          () => api.setActive(user.username, false),
          `${user.name} has been deactivated.`,
        ),
    },
  };

  return (
    <div
      className="drawer-backdrop"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`Manage ${user.name}`}
        ref={drawer}
        tabIndex={-1}
      >
        <header className="drawer-header">
          <div className="user-cell large">
            <Avatar name={user.name} size={44} />
            <div>
              <strong>{user.name}</strong>
              <span>
                {user.username} · {user.role_label || roleName(user.role)}
              </span>
            </div>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            <Icon name="x" />
          </button>
        </header>
        <div className="drawer-body">
          <Alert>{error}</Alert>
          <Alert kind="success">{notice}</Alert>

          <div className="drawer-row">
            <h3 className="drawer-section">Details</h3>
            <button
              className="link-button"
              onClick={() => setEditing(true)}
              disabled={!!busy}
            >
              Edit
            </button>
          </div>
          <dl className="detail-grid">
            <div>
              <dt>Status</dt>
              <dd>
                <Badge tone={s.tone} dot>
                  {s.label}
                </Badge>
                {user.locked_until && (
                  <span className="muted small">
                    {" "}
                    until {formatDateTime(user.locked_until)}
                  </span>
                )}
              </dd>
            </div>
            <div>
              <dt>Department</dt>
              <dd>{user.department || "—"}</dd>
            </div>
            <div>
              <dt>Last sign-in</dt>
              <dd>{formatDateTime(user.last_login_at)}</dd>
            </div>
            <div>
              <dt>Password changed</dt>
              <dd>{formatDateTime(user.password_changed_at)}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>{formatDateTime(user.created_at)}</dd>
            </div>
          </dl>

          <h3 className="drawer-section">Account actions</h3>
          <div className="action-list">
            <ActionRow
              icon="key"
              title="Reset password"
              text="Set a new password. They are signed out everywhere."
              button="Reset"
              onClick={() => setResetting(true)}
              disabled={!!busy}
            />
            <ActionRow
              icon="logout"
              title="Sign out everywhere"
              text="End every browser and MCP session."
              button="Sign out"
              onClick={() => setConfirm(actions.signOut)}
              disabled={!!busy}
            />
            {user.locked_until && (
              <ActionRow
                icon="lock"
                title="Unlock"
                text="Clear the lock from repeated failed sign-ins."
                button="Unlock"
                busy={busy === "unlock"}
                onClick={() =>
                  run(
                    "unlock",
                    () => api.unlock(user.username),
                    `${user.name} can sign in again.`,
                  )
                }
                disabled={!!busy}
              />
            )}
            {user.active ? (
              <ActionRow
                icon="x"
                title="Deactivate"
                text={
                  isMe
                    ? "You cannot deactivate your own account."
                    : "Block sign-in and end all sessions."
                }
                button="Deactivate"
                danger
                onClick={() => setConfirm(actions.deactivate)}
                disabled={!!busy || isMe}
              />
            ) : (
              <ActionRow
                icon="check"
                title="Reactivate"
                text="Allow this account to sign in again."
                button="Reactivate"
                busy={busy === "activate"}
                onClick={() =>
                  run(
                    "activate",
                    () => api.setActive(user.username, true),
                    `${user.name} is active again.`,
                  )
                }
                disabled={!!busy}
              />
            )}
          </div>

          <h3 className="drawer-section">Sessions</h3>
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
                    <strong>{sessionLabel(session)}</strong>
                    <span>
                      Active {timeAgo(session.last_used_at).toLowerCase()} ·
                      signed in {formatDateTime(session.created_at)}
                    </span>
                  </div>
                  <button
                    className="button ghost small"
                    disabled={!!busy}
                    onClick={() =>
                      run(
                        `end-${session.id}`,
                        () => api.endUserSession(user.username, session.id),
                        "The session has been ended.",
                      )
                    }
                  >
                    End
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">No active sessions.</p>
          )}
        </div>
      </aside>

      {confirm && (
        <Modal
          title={confirm.title}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <button
                className="button secondary"
                onClick={() => setConfirm(null)}
                disabled={!!busy}
              >
                Cancel
              </button>
              <button
                className={`button ${confirm.danger ? "danger" : "primary"}`}
                onClick={confirm.run}
                disabled={!!busy}
              >
                {busy && <Spinner />}
                {confirm.button}
              </button>
            </>
          }
        >
          <p className="modal-text">{confirm.body}</p>
        </Modal>
      )}
      {editing && (
        <EditProfile
          user={user}
          onClose={() => setEditing(false)}
          onSaved={async () => {
            setEditing(false);
            setNotice("Details saved.");
            await onChanged();
          }}
        />
      )}
      {resetting && (
        <ResetPassword
          user={user}
          onClose={() => setResetting(false)}
          onDone={async (password) => {
            setResetting(false);
            setNotice(
              `${user.name}’s password was changed and they were signed out everywhere.`,
            );
            await Promise.all([onChanged(), loadSessions()]);
            if (password) onCredentials({ username: user.username, password });
          }}
        />
      )}
    </div>
  );
}

function ActionRow({
  icon,
  title,
  text,
  button,
  onClick,
  disabled,
  danger,
  busy,
}) {
  return (
    <div className="action-row">
      <span className={`action-icon ${danger ? "danger" : ""}`}>
        <Icon name={icon} size={15} />
      </span>
      <div>
        <strong>{title}</strong>
        <span>{text}</span>
      </div>
      <button
        className={`button small ${danger ? "danger-outline" : "secondary"}`}
        onClick={onClick}
        disabled={disabled}
      >
        {busy && <Spinner />}
        {button}
      </button>
    </div>
  );
}

function EditProfile({ user, onClose, onSaved }) {
  const [name, setName] = useState(user.name);
  const [department, setDepartment] = useState(user.department || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.updateUser(user.username, { name, department });
      await onSaved();
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Edit ${user.username}`}
      description="The name appears in the app and on answers they send for review."
      onClose={onClose}
    >
      <form onSubmit={submit} className="stack">
        <Alert>{error}</Alert>
        <div className="field">
          <label htmlFor="edit-name">Full name</label>
          <input
            id="edit-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={80}
          />
        </div>
        <div className="field">
          <label htmlFor="edit-department">
            Department <span className="muted">(optional)</span>
          </label>
          <input
            id="edit-department"
            value={department}
            onChange={(e) => setDepartment(e.target.value)}
            maxLength={80}
          />
        </div>
        <div className="modal-footer inline">
          <button
            type="button"
            className="button secondary"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </button>
          <button className="button primary" disabled={busy || !name.trim()}>
            {busy && <Spinner />}
            Save
          </button>
        </div>
      </form>
    </Modal>
  );
}

function ResetPassword({ user, onClose, onDone }) {
  const [mode, setMode] = useState("generate");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api.resetPassword(
        user.username,
        mode === "choose" ? password : null,
      );
      await onDone(result.password);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Reset ${user.name}’s password`}
      description="They will be signed out of every browser and MCP client."
      onClose={onClose}
    >
      <form onSubmit={submit} className="stack">
        <Alert>{error}</Alert>
        <div
          className="choice-group"
          role="radiogroup"
          aria-label="New password"
        >
          <label className={`choice ${mode === "generate" ? "selected" : ""}`}>
            <input
              type="radio"
              name="mode"
              checked={mode === "generate"}
              onChange={() => setMode("generate")}
            />
            <div>
              <strong>Generate a strong password</strong>
              <span>Shown once, ready to copy and share securely.</span>
            </div>
          </label>
          <label className={`choice ${mode === "choose" ? "selected" : ""}`}>
            <input
              type="radio"
              name="mode"
              checked={mode === "choose"}
              onChange={() => setMode("choose")}
            />
            <div>
              <strong>Set a password</strong>
              <span>At least 12 characters.</span>
            </div>
          </label>
        </div>
        {mode === "choose" && (
          <div className="field">
            <label htmlFor="new-user-password">New password</label>
            <input
              id="new-user-password"
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={12}
            />
          </div>
        )}
        <div className="modal-footer inline">
          <button
            type="button"
            className="button secondary"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            className="button primary"
            disabled={busy || (mode === "choose" && password.length < 12)}
          >
            {busy && <Spinner />}
            Reset password
          </button>
        </div>
      </form>
    </Modal>
  );
}

function AddUser({ onClose, onCreated }) {
  const [form, setForm] = useState({
    username: "",
    name: "",
    role: "analyst",
    department: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onCreated(
        await api.createUser({
          ...form,
          username: form.username.trim().toLowerCase(),
        }),
      );
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Add user"
      description="A strong password is generated for them; you’ll see it once."
      onClose={onClose}
    >
      <form onSubmit={submit} className="stack">
        <Alert>{error}</Alert>
        <div className="field-row">
          <div className="field">
            <label htmlFor="add-name">Full name</label>
            <input
              id="add-name"
              value={form.name}
              onChange={set("name")}
              required
              maxLength={80}
            />
          </div>
          <div className="field">
            <label htmlFor="add-username">Username</label>
            <input
              id="add-username"
              value={form.username}
              onChange={set("username")}
              required
              autoCapitalize="none"
              spellCheck={false}
              pattern="[A-Za-z0-9][A-Za-z0-9._\-]{1,39}"
              title="2 to 40 characters: letters, digits, '.', '_' or '-'"
            />
          </div>
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="add-role">Role</label>
            <select id="add-role" value={form.role} onChange={set("role")}>
              {Object.entries(ROLE_NAMES).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="add-department">
              Department <span className="muted">(optional)</span>
            </label>
            <input
              id="add-department"
              value={form.department}
              onChange={set("department")}
              maxLength={80}
            />
          </div>
        </div>
        <p className="muted small">
          The role decides which documents they can search, in the app and from
          MCP clients.
        </p>
        <div className="modal-footer inline">
          <button
            type="button"
            className="button secondary"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            className="button primary"
            disabled={busy || !form.name.trim() || !form.username.trim()}
          >
            {busy && <Spinner />}
            Create user
          </button>
        </div>
      </form>
    </Modal>
  );
}

function Credentials({ username, password, created, onClose }) {
  const sign = `${window.location.origin}/`;
  return (
    <Modal
      title={created ? "User created" : "New password"}
      description="Copy these now. The password is not stored in a readable form and won’t be shown again."
      onClose={onClose}
      footer={
        <button className="button primary" onClick={onClose}>
          Done
        </button>
      }
    >
      <div className="stack">
        <div className="field">
          <span className="field-label">Sign-in address</span>
          <CopyField value={sign} label="Copy sign-in address" />
        </div>
        <div className="field">
          <span className="field-label">Username</span>
          <CopyField value={username} label="Copy username" />
        </div>
        <div className="field">
          <span className="field-label">Password</span>
          <CopyField value={password} label="Copy password" />
        </div>
        <Alert kind="info">
          Share the password through a private channel. They can change it under
          Account.
        </Alert>
      </div>
    </Modal>
  );
}
