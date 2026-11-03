import { useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "./api.js";

export const roleName = (role) =>
  ({
    analyst: "Analyst",
    finance: "Finance",
    hr: "HR",
    c_level: "Management",
    admin: "Admin",
  })[role] || role;

export function Icon({ name, size = 18 }) {
  const paths = {
    chart: (
      <>
        <path d="M5 19v-6M12 19V8M19 19V4" />
        <path d="M3 22h19" />
      </>
    ),
    plus: <path d="M12 5v14M5 12h14" />,
    send: (
      <>
        <path d="m4 4 17 8-17 8 3-8-3-8Z" />
        <path d="M7 12h14" />
      </>
    ),
    chat: (
      <path d="M20 15a3 3 0 0 1-3 3H8l-5 3V6a3 3 0 0 1 3-3h11a3 3 0 0 1 3 3Z" />
    ),
    shield: (
      <>
        <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z" />
        <path d="m8 12 3 3 5-6" />
      </>
    ),
    logout: (
      <>
        <path d="M9 4H4v16h5M14 8l4 4-4 4M9 12h12" />
      </>
    ),
    file: (
      <>
        <path d="M14 3H5v18h14V8ZM14 3v5h5M8 13h8M8 17h6" />
      </>
    ),
    arrow: <path d="m8 5 7 7-7 7" />,
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}

export function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <Icon name="chart" size={21} />
      </span>
      <span>
        Financial Research<span className="brand-sub">ASSISTANT</span>
      </span>
    </div>
  );
}

export function Alert({ children, kind = "error" }) {
  return children ? (
    <div
      className={`alert ${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  ) : null;
}

export function Answer({ children }) {
  return (
    <div className="answer">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          img: () => null,
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {children || ""}
      </Markdown>
    </div>
  );
}

export function RequestDetails({ requestId }) {
  const [copied, setCopied] = useState("");
  const path = requestId ? `logs/traces/${requestId}/summary.md` : "";
  async function copy(value, label) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
    } catch {
      setCopied("Select the text below to copy it.");
    }
  }
  return (
    <details className="request-details">
      <summary>Request details</summary>
      {requestId ? (
        <>
          <div className="request-detail-row">
            <span>Request ID</span>
            <button
              className="text-button"
              onClick={() => copy(requestId, "Request ID copied")}
            >
              Copy ID
            </button>
          </div>
          <code>{requestId}</code>
          <div className="request-detail-row">
            <span>Local report</span>
            <button
              className="text-button"
              onClick={() => copy(path, "Report path copied")}
            >
              Copy path
            </button>
          </div>
          <code>{path}</code>
          <small>Open this path in VS Code from the project folder.</small>
          {copied && <small role="status">{copied}</small>}
        </>
      ) : (
        <small>
          No request ID is available yet. Older saved questions may not have
          one.
        </small>
      )}
    </details>
  );
}

export function Sources({ sources = [] }) {
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const unique = [
    ...new Map(sources.map((s) => [`${s.file}:${s.page}`, s])).values(),
  ];
  async function download(file) {
    setError("");
    setLoading(true);
    try {
      await api.download(file);
    } catch (e) {
      setError(
        e.response?.status === 404
          ? "The original PDF is not stored locally. Its filename and page reference remain available here."
          : "This source PDF could not be opened. Your role may not have access.",
      );
    } finally {
      setLoading(false);
    }
  }
  if (!unique.length) return null;
  return (
    <div className="sources">
      <div className="section-label">SOURCES · {unique.length}</div>
      <div className="source-list">
        {unique.map((s) => (
          <button
            className="source"
            key={`${s.file}:${s.page}`}
            onClick={() => download(s.file)}
            disabled={loading}
            title="Download original PDF if available locally"
          >
            <Icon name="file" size={16} />
            <span>
              {s.file}
              <small>Page {s.page ?? "—"}</small>
            </span>
          </button>
        ))}
      </div>
      <Alert kind="info">{error}</Alert>
    </div>
  );
}

export function Login({ onLogin }) {
  const [username, setUsername] = useState("analyst");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await onLogin(username, password);
    } catch (e) {
      setError(
        e.response?.status === 401
          ? "The username or password is incorrect."
          : "Unable to sign in. Check that the backend is running.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-page">
      <section className="login-intro">
        <Brand />
        <div className="login-pitch">
          <div className="eyebrow">DOCUMENT-BASED FINANCIAL RESEARCH</div>
          <h1>
            Better questions.
            <br />
            Clearer evidence.
          </h1>
          <p>
            Explore company filings, compare financial figures, and trace every
            answer back to its sources.
          </p>
          <div className="intro-rule" />
          <div className="intro-facts">
            <span>
              <strong>32</strong>companies
            </span>
            <span>
              <strong>84</strong>filings
            </span>
            <span>
              <Icon name="shield" size={25} />
              Role-based access
            </span>
          </div>
        </div>
        <span className="login-footnote">
          Financial Research Assistant · Local workspace
        </span>
      </section>
      <section className="login-form-area">
        <form className="login-card" onSubmit={submit}>
          <span className="section-label">YOUR RESEARCH WORKSPACE</span>
          <h2>Welcome back</h2>
          <p className="muted">Sign in to continue your research.</p>
          <Alert>{error}</Alert>
          <label htmlFor="username">Username</label>
          <input
            id="username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            disabled={busy}
          />
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            placeholder="Enter your password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            disabled={busy}
          />
          <button className="button primary login-submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
            <Icon name="arrow" />
          </button>
          <div className="demo-accounts">
            <label htmlFor="demo-account">Demo accounts</label>
            <select
              id="demo-account"
              defaultValue=""
              disabled={busy}
              onChange={(e) => {
                if (e.target.value) {
                  setUsername(e.target.value);
                  setPassword(`${e.target.value}123`);
                }
              }}
            >
              <option value="" disabled>
                Fill a demo account
              </option>
              {["analyst", "finance", "hr", "clevel", "admin"].map((u) => (
                <option key={u} value={u}>
                  {roleName(u === "clevel" ? "c_level" : u)}
                </option>
              ))}
            </select>
            <small>
              Fixed demo accounts let you explore the different access roles.
            </small>
          </div>
        </form>
      </section>
    </main>
  );
}
