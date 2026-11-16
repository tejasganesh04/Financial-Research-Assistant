import { useEffect, useId, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "./api.js";

export const ROLE_NAMES = {
  analyst: "Analyst",
  finance: "Finance",
  hr: "HR",
  c_level: "Executive",
  admin: "Administrator",
};
export const roleName = (role) => ROLE_NAMES[role] || role;

export const initials = (name = "") =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("") || "?";

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
export function timeAgo(value) {
  if (!value) return "Never";
  const seconds = (new Date(value).getTime() - Date.now()) / 1000;
  const steps = [
    [60, "second"],
    [60, "minute"],
    [24, "hour"],
    [7, "day"],
    [4.35, "week"],
    [12, "month"],
    [Infinity, "year"],
  ];
  let amount = seconds;
  for (const [size, unit] of steps) {
    if (Math.abs(amount) < size) {
      if (unit === "second") return "Just now";
      return relative.format(Math.round(amount), unit);
    }
    amount /= size;
  }
  return "";
}

export const formatDateTime = (value) =>
  value
    ? new Date(value).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : "—";

const ICONS = {
  chart: (
    <>
      <path d="M4 19V13M10 19V9M16 19V5" />
      <path d="M3 21h18" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  send: <path d="M5 12h13M13 6l6 6-6 6" />,
  chat: (
    <path d="M20 15a3 3 0 0 1-3 3H8l-5 3V6a3 3 0 0 1 3-3h11a3 3 0 0 1 3 3Z" />
  ),
  shield: (
    <>
      <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z" />
      <path d="m9 12 2 2 4-4" />
    </>
  ),
  logout: <path d="M10 4H5v16h5M15 8l4 4-4 4M9 12h10" />,
  file: <path d="M14 3H6v18h12V7ZM14 3v4h4M9 13h6M9 17h4" />,
  arrow: <path d="m9 6 6 6-6 6" />,
  back: <path d="m15 6-6 6 6 6" />,
  refresh: (
    <>
      <path d="M20 5v5h-5M4 19v-5h5" />
      <path d="M6.5 9A7 7 0 0 1 19 9.5M17.5 15A7 7 0 0 1 5 14.5" />
    </>
  ),
  check: <path d="m5 12 4.5 4.5L19 7" />,
  users: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0 1 16 0" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="m11 12 9-9M17 6l3 3M14 9l2 2" />
    </>
  ),
  terminal: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="m7 9 3 3-3 3M13 15h4" />
    </>
  ),
  plug: (
    <>
      <path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0Z" />
      <path d="M12 17v4" />
    </>
  ),
  copy: (
    <>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V6a2 2 0 0 1 2-2h8" />
    </>
  ),
  x: <path d="M6 6l12 12M18 6 6 18" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  lock: (
    <>
      <rect x="5" y="11" width="14" height="10" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </>
  ),
  activity: <path d="M3 12h4l3 7 4-14 3 7h4" />,
  monitor: (
    <>
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </>
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1" />
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
    </>
  ),
  trash: <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8h.01" />
    </>
  ),
  alert: (
    <>
      <path d="M12 3 2 20h20Z" />
      <path d="M12 10v4M12 17h.01" />
    </>
  ),
  sparkle: (
    <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" />
  ),
  external: <path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6" />,
};

export function Icon({ name, size = 16 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="icon"
    >
      {ICONS[name]}
    </svg>
  );
}

export function Logo({ size = 28 }) {
  return (
    <span
      className="logo"
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      <svg viewBox="0 0 28 28" width={size} height={size}>
        <rect width="28" height="28" rx="7" className="logo-bg" />
        <path d="M9 19v-3.5M14 19v-7" className="logo-bars" />
        <path d="M19 19V8" className="logo-line" />
      </svg>
    </span>
  );
}

export function Brand({ compact = false }) {
  return (
    <div className="brand">
      <Logo />
      {!compact && (
        <span className="brand-name">
          Financial Research
          <small>Assistant</small>
        </span>
      )}
    </div>
  );
}

export function Alert({ children, kind = "error", title }) {
  if (!children) return null;
  const icon = {
    error: "alert",
    success: "check",
    info: "info",
    warning: "alert",
  }[kind];
  return (
    <div
      className={`alert ${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      <Icon name={icon} size={16} />
      <div>
        {title && <strong>{title}</strong>}
        <span>{children}</span>
      </div>
    </div>
  );
}

export function Spinner({ size = 14 }) {
  return (
    <span
      className="spinner"
      style={{ width: size, height: size }}
      aria-hidden="true"
    />
  );
}

export function Avatar({ name, size = 32 }) {
  return (
    <span
      className="avatar"
      style={{ width: size, height: size, fontSize: size * 0.38 }}
      aria-hidden="true"
    >
      {initials(name)}
    </span>
  );
}

export function Badge({ children, tone = "neutral", dot = false }) {
  return (
    <span className={`badge ${tone}`}>
      {dot && <span className="badge-dot" />}
      {children}
    </span>
  );
}

export function CopyField({ value, label, mono = true, multiline = false }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef();
  useEffect(() => () => clearTimeout(timer.current), []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className={`copy-field ${multiline ? "multiline" : ""}`}>
      <code className={mono ? "mono" : ""}>{value}</code>
      <button
        type="button"
        className="icon-button"
        onClick={copy}
        aria-label={label || "Copy"}
      >
        <Icon name={copied ? "check" : "copy"} size={15} />
      </button>
      <span className="sr-only" role="status">
        {copied ? "Copied" : ""}
      </span>
    </div>
  );
}

export function Modal({
  title,
  description,
  onClose,
  children,
  footer,
  width = 460,
}) {
  const titleId = useId();
  const panel = useRef();
  useEffect(() => {
    const previous = document.activeElement;
    const first = panel.current?.querySelector(
      "input, select, textarea, button:not(.modal-close)",
    );
    (first || panel.current)?.focus();
    const onKey = (event) => {
      if (event.key === "Escape") onClose?.();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={panel}
        tabIndex={-1}
        style={{ maxWidth: width }}
      >
        <header className="modal-header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && <p>{description}</p>}
          </div>
          {onClose && (
            <button
              className="icon-button modal-close"
              onClick={onClose}
              aria-label="Close"
            >
              <Icon name="x" />
            </button>
          )}
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-footer">{footer}</footer>}
      </div>
    </div>
  );
}

export function EmptyState({ icon, title, children, action }) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon name={icon} size={20} />
      </span>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function PageHeader({ title, description, actions, eyebrow }) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
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
          table: ({ children }) => (
            <div className="table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {children || ""}
      </Markdown>
    </div>
  );
}

export function RequestDetails({ requestId }) {
  const path = requestId ? `logs/traces/${requestId}/summary.md` : "";
  return (
    <details className="request-details">
      <summary>Request details</summary>
      {requestId ? (
        <div className="request-details-body">
          <span>Request ID</span>
          <CopyField value={requestId} label="Copy request ID" />
          <span>Server report</span>
          <CopyField value={path} label="Copy report path" />
        </div>
      ) : (
        <small>
          No request ID is available. Older saved questions may not have one.
        </small>
      )}
    </details>
  );
}

export function Sources({ sources = [] }) {
  const [error, setError] = useState("");
  const [loading, setLoading] = useState("");
  const unique = [
    ...new Map(sources.map((s) => [`${s.file}:${s.page}`, s])).values(),
  ];
  async function download(file) {
    setError("");
    setLoading(file);
    try {
      await api.download(file);
    } catch (e) {
      setError(
        e.response?.status === 404
          ? "The original PDF is not stored on this server. The document name and page remain available here."
          : "This source PDF could not be opened. Your role may not have access.",
      );
    } finally {
      setLoading("");
    }
  }
  if (!unique.length) return null;
  return (
    <div className="sources">
      <div className="sources-label">
        Sources <span>{unique.length}</span>
      </div>
      <div className="source-list">
        {unique.map((s, index) => (
          <button
            className="source"
            key={`${s.file}:${s.page}`}
            onClick={() => download(s.file)}
            disabled={!!loading}
            title="Download the original PDF, if stored on this server"
          >
            <span className="source-index">{index + 1}</span>
            <span className="source-text">
              <span className="source-file">{s.file}</span>
              <small>Page {s.page ?? "—"}</small>
            </span>
            {loading === s.file ? <Spinner /> : <Icon name="file" size={15} />}
          </button>
        ))}
      </div>
      <Alert kind="info">{error}</Alert>
    </div>
  );
}
