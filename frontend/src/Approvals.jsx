import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api.js";
import {
  Alert,
  Answer,
  Badge,
  DOC_TYPES,
  EmptyState,
  Icon,
  PageHeader,
  Sources,
  Spinner,
  roleName,
} from "./components.jsx";

export default function Approvals({ onCountChange }) {
  const [items, setItems] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState(null);
  const detail = useRef(null);
  // On a phone the details sit below the queue: bring them into view when a request opens.
  useEffect(() => {
    if (selected && window.matchMedia("(max-width: 960px)").matches)
      detail.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [selected?.thread_id]);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function load() {
    const approvals = await api.approvals();
    setItems(approvals);
    onCountChange?.(approvals.length);
  }

  async function refresh() {
    setBusy("loading");
    setError("");
    try {
      await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
      setLoaded(true);
    }
  }
  useEffect(() => {
    refresh();
  }, []);

  async function open(id) {
    setBusy("opening");
    setError("");
    setNotice("");
    setSelected(null);
    setReason("");
    try {
      setSelected(await api.approval(id));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }

  async function decide(decision) {
    if (!selected || busy) return;
    if (decision === "reject" && !reason.trim()) {
      setError("Add a reason before rejecting this answer.");
      return;
    }
    setBusy(decision);
    setError("");
    try {
      await api.decide(selected.thread_id, decision, reason.trim());
      setNotice(
        decision === "approve"
          ? "Answer approved and released to the requester."
          : "Answer rejected. The requester will see your reason.",
      );
      setSelected(null);
      setReason("");
      await load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="Approvals"
        description="Answers that used access beyond the requester’s role. Review the draft and its sources before releasing it."
        actions={
          <button
            className="button secondary"
            onClick={refresh}
            disabled={!!busy}
          >
            <Icon name="refresh" size={14} />
            Refresh
          </button>
        }
      />
      <Alert>{error}</Alert>
      <Alert kind="success">{notice}</Alert>
      <div className="review-layout">
        <section className="panel review-queue" aria-label="Pending requests">
          <div className="panel-heading">
            <span>Pending</span>
            <Badge>{items.length}</Badge>
          </div>
          {items.length ? (
            <div className="queue-list">
              {items.map((item) => (
                <button
                  className={`queue-item ${selected?.thread_id === item.thread_id ? "selected" : ""}`}
                  key={item.thread_id}
                  onClick={() => open(item.thread_id)}
                  disabled={!!busy}
                >
                  <strong>{item.query}</strong>
                  <span>
                    {item.requester_name || item.requester_user_id} ·{" "}
                    {roleName(item.requester_role)} · {item.sources_count}{" "}
                    {item.sources_count === 1 ? "source" : "sources"}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <EmptyState
              icon="check"
              title={!loaded ? "Loading requests…" : "All caught up"}
            >
              {loaded && "No answers are waiting for review."}
            </EmptyState>
          )}
        </section>

        <section className="panel review-detail" ref={detail}>
          {selected ? (
            <>
              <div className="review-meta">
                <Badge tone="warning" dot>
                  Awaiting review
                </Badge>
                <span>
                  {selected.requester_name} ·{" "}
                  {roleName(selected.requester_role)}
                  {selected.requester_department
                    ? ` · ${selected.requester_department}`
                    : ""}
                </span>
              </div>
              <h2 className="review-question">{selected.query}</h2>
              {selected.reason && <p className="muted">{selected.reason}</p>}
              {selected.access_scope && (
                <dl className="scope-list">
                  <div>
                    <dt>Document types searched</dt>
                    <dd>
                      {selected.access_scope.doc_types
                        .map((d) => DOC_TYPES[d] || d)
                        .join(", ")}
                    </dd>
                  </div>
                  <div>
                    <dt>Confidentiality</dt>
                    <dd>
                      {selected.access_scope.confidentiality
                        .map((c) => c[0].toUpperCase() + c.slice(1))
                        .join(", ")}
                    </dd>
                  </div>
                </dl>
              )}
              {selected.trace_url && (
                <a
                  className="trace-link"
                  href={selected.trace_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <Icon name="external" size={14} /> Open the trace in LangSmith
                </a>
              )}
              <div className="draft">
                <div className="draft-label">Draft answer</div>
                <Answer>{selected.draft_answer}</Answer>
                <Sources sources={selected.sources} />
              </div>
              <div className="decision">
                <div className="field">
                  <label htmlFor="review-reason">
                    Review note{" "}
                    <span className="muted">(required to reject)</span>
                  </label>
                  <textarea
                    id="review-reason"
                    rows={3}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    disabled={!!busy}
                    placeholder="Explain your decision…"
                  />
                </div>
                <div className="decision-actions">
                  <span className="muted small">
                    Approving releases this answer only; it does not change the
                    role.
                  </span>
                  <button
                    className="button danger-outline"
                    onClick={() => decide("reject")}
                    disabled={!!busy}
                  >
                    {busy === "reject" && <Spinner />}
                    Reject answer
                  </button>
                  <button
                    className="button primary"
                    onClick={() => decide("approve")}
                    disabled={!!busy}
                  >
                    {busy === "approve" ? (
                      <Spinner />
                    ) : (
                      <Icon name="check" size={15} />
                    )}
                    Approve & release
                  </button>
                </div>
              </div>
            </>
          ) : (
            <EmptyState
              icon="shield"
              title={
                busy === "opening" ? "Opening request…" : "Select a request"
              }
            >
              The question, draft answer and its sources will appear here.
            </EmptyState>
          )}
        </section>
      </div>
    </div>
  );
}
