import { useEffect, useState } from "react";
import { api, errorMessage } from "./api.js";
import {
  Alert,
  Answer,
  Icon,
  Sources,
  RequestDetails,
  roleName,
} from "./components.jsx";

// ADMIN REVIEW: Load pending requests and their drafts; submit approve/reject without starting another answer.
export default function Approvals() {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function refresh() {
    setBusy("loading");
    setError("");
    try {
      setItems(await api.approvals());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
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
    setBusy("deciding");
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
      setItems(await api.approvals());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="approvals-page">
      <div className="approvals-intro">
        <div>
          <h2>Pending approvals</h2>
          <p>
            Review the question, draft and supporting sources before making a
            decision.
          </p>
        </div>
        <button
          className="button secondary"
          onClick={refresh}
          disabled={!!busy}
        >
          <Icon name="refresh" size={15} />
          Refresh
        </button>
      </div>
      <Alert>{error}</Alert>
      <Alert kind="success">{notice}</Alert>
      <div className="approval-layout">
        <section className="approval-list" aria-label="Pending requests">
          {items.length ? (
            items.map((item) => (
              <button
                className={`approval-item ${selected?.thread_id === item.thread_id ? "selected" : ""}`}
                key={item.thread_id}
                onClick={() => open(item.thread_id)}
                disabled={!!busy}
              >
                <span className="approval-item-meta">
                  <span className="pill amber">Awaiting review</span>
                  <span>{roleName(item.requester_role)}</span>
                </span>
                <strong>{item.query}</strong>
                <small>
                  {item.requester_name || item.requester_user_id} ·{" "}
                  {item.sources_count} sources
                </small>
              </button>
            ))
          ) : (
            <div className="empty-list">
              <Icon name="check" size={25} />
              <h3>
                {busy === "loading" ? "Loading requests…" : "All caught up"}
              </h3>
              <p>No pending requests available for your role.</p>
            </div>
          )}
        </section>
        <section className="review-panel">
          {selected ? (
            <>
              <div className="review-top">
                <span className="section-label">
                  REQUEST FROM {roleName(selected.requester_role).toUpperCase()}
                </span>
                <span className="pill amber">Awaiting review</span>
              </div>
              <h2>{selected.query}</h2>
              <p className="muted">
                {selected.requester_name} · {selected.requester_department}
              </p>
              <p className="muted">{selected.reason}</p>
              {selected.access_scope && (
                <p className="muted">
                  Effective search: {selected.access_scope.doc_types.join(", ")}{" "}
                  · {selected.access_scope.confidentiality.join(", ")}. Approval
                  releases this answer only.
                </p>
              )}
              <RequestDetails requestId={selected.request_id} />
              <div className="review-rule" />
              <h3 className="section-label">DRAFT ANSWER</h3>
              <Answer>{selected.draft_answer}</Answer>
              <Sources sources={selected.sources} />
              <div className="decision-form">
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
                <div className="decision-buttons">
                  <button
                    className="button danger-outline"
                    onClick={() => decide("reject")}
                    disabled={!!busy}
                  >
                    Reject answer
                  </button>
                  <button
                    className="button primary"
                    onClick={() => decide("approve")}
                    disabled={!!busy}
                  >
                    <Icon name="check" size={16} />
                    {busy === "deciding" ? "Saving…" : "Approve & release"}
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="review-placeholder">
              <Icon name="shield" size={33} />
              <h3>
                {busy === "opening"
                  ? "Opening request…"
                  : "Select a request to review"}
              </h3>
              <p>The draft answer and its sources will appear here.</p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
