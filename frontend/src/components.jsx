import { useEffect, useId, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "./api.js";
import { ICON_PATHS } from "./icons.js";

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

export function Icon({ name, size = 16 }) {
  // Material Symbols are drawn for a 20-24px box; callers pass the old, smaller optical sizes.
  const box = Math.round(size * 1.25);
  return (
    <svg
      width={box}
      height={box}
      viewBox="0 -960 960 960"
      fill="currentColor"
      aria-hidden="true"
      className="icon"
    >
      <path d={ICON_PATHS[name]} />
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
      <svg viewBox="0 0 32 32" width={size} height={size}>
        <rect
          x="3"
          y="17"
          width="7"
          height="12"
          rx="3.5"
          className="logo-bar-1"
        />
        <rect
          x="12.5"
          y="10"
          width="7"
          height="19"
          rx="3.5"
          className="logo-bar-2"
        />
        <rect
          x="22"
          y="3"
          width="7"
          height="26"
          rx="3.5"
          className="logo-bar-3"
        />
      </svg>
    </span>
  );
}

export function Brand() {
  return (
    <div className="brand">
      <Logo />
      <span className="brand-name">Financial Research</span>
    </div>
  );
}

export function Alert({ children, kind = "error", title }) {
  if (!children) return null;
  const icon = {
    error: "error",
    success: "done",
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

// Each initial has its own colour, as in an address book.
const AVATAR_COLORS = [
  "#1a73e8",
  "#b06000",
  "#188038",
  "#8430ce",
  "#007b83",
  "#b80672",
  "#3f51b5",
  "#5f6368",
];

export function Avatar({ name = "", size = 32 }) {
  const initial = initials(name)[0];
  return (
    <span
      className="avatar"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.45),
        "--avatar":
          AVATAR_COLORS[initial.codePointAt(0) % AVATAR_COLORS.length],
      }}
      aria-hidden="true"
    >
      {initial}
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
