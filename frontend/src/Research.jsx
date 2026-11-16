import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api.js";
import {
  Alert,
  Answer,
  Icon,
  Logo,
  Modal,
  RequestDetails,
  Sources,
  Spinner,
} from "./components.jsx";

// The selected conversation ID survives a reload in this tab. Messages are loaded from the server.
export const THREAD_KEY = "financial-research-conversation";
const emptyConversation = () => ({ id: null, messages: [], pending: false });
const fromHistory = (data) => ({
  id: data.thread_id,
  pending: data.is_interrupted,
  approval: data.interrupt_payload,
  messages: data.messages.map((m, i) => ({
    ...m,
    id: `${data.thread_id}-${i}`,
  })),
});

const SUGGESTIONS = [
  [
    "search",
    "Find a figure",
    "What was Microsoft’s cost of goods sold in FY2016? Give the answer in USD millions.",
  ],
  [
    "compare",
    "Compare years",
    "Compare Microsoft’s total revenue in FY2016 and FY2023. Cite each year’s filing.",
  ],
  [
    "calculate",
    "Explain a calculation",
    "How do Microsoft’s FY2016 revenue and cost of revenue reconcile to gross profit? Show the calculation.",
  ],
];

// Conversation state lives above the views so it survives moving between pages.
export function useResearch(user) {
  const [history, setHistory] = useState([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [thread, setThread] = useState(emptyConversation);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const activeRequest = useRef(null);
  const historyVersion = useRef(0);

  async function refreshHistory() {
    try {
      setHistory(await api.conversations());
    } catch (e) {
      if (e.response?.status !== 401) setError(errorMessage(e));
    } finally {
      setHistoryLoaded(true);
    }
  }

  async function openConversation(id) {
    if (activeRequest.current) return;
    const version = ++historyVersion.current;
    setBusy("Opening conversation…");
    setError("");
    setNotice("");
    try {
      const data = await api.conversation(id);
      if (version !== historyVersion.current) return;
      setThread(fromHistory(data));
      setQuestion("");
      sessionStorage.setItem(THREAD_KEY, id);
    } catch (e) {
      if (version === historyVersion.current) {
        if (e.response?.status === 404 || e.response?.status === 403) {
          sessionStorage.removeItem(THREAD_KEY);
          setThread(emptyConversation());
        } else setError(errorMessage(e));
      }
    } finally {
      if (version === historyVersion.current) setBusy("");
    }
  }

  function newConversation() {
    if (activeRequest.current) return;
    historyVersion.current += 1;
    setBusy("");
    setThread(emptyConversation());
    setQuestion("");
    setError("");
    setNotice("");
    sessionStorage.removeItem(THREAD_KEY);
  }

  async function deleteConversation(id) {
    await api.deleteConversation(id);
    if (thread.id === id) newConversation();
    await refreshHistory();
  }

  function reset() {
    activeRequest.current?.abort();
    activeRequest.current = null;
    historyVersion.current += 1;
    sessionStorage.removeItem(THREAD_KEY);
    setHistory([]);
    setThread(emptyConversation());
    setQuestion("");
    setBusy("");
    setError("");
    setNotice("");
  }

  useEffect(() => {
    if (!user) return;
    refreshHistory();
    const saved = sessionStorage.getItem(THREAD_KEY);
    if (saved) openConversation(saved);
  }, [user?.user_id]);

  // Only answers held for review are polled; these reads never start another answer.
  useEffect(() => {
    if (!user || !thread.id || !thread.pending) return;
    let cancelled = false;
    let timer;
    async function poll() {
      try {
        const result = await api.result(thread.id);
        if (cancelled) return;
        if (["ready", "approved", "rejected"].includes(result.status)) {
          const data = await api.conversation(thread.id);
          if (!cancelled) {
            setThread(fromHistory(data));
            setNotice(
              result.status === "rejected"
                ? "The reviewer rejected this answer."
                : "The reviewed answer is now available.",
            );
            refreshHistory();
          }
          return;
        }
      } catch {
        if (!cancelled)
          setError(
            "Could not check the review status. You can refresh this conversation.",
          );
      }
      if (!cancelled) timer = setTimeout(poll, 4000);
    }
    timer = setTimeout(poll, 1500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [user?.user_id, thread.id, thread.pending]);

  async function submit(onFollow) {
    if (!question.trim() || activeRequest.current || busy || thread.pending)
      return;
    onFollow?.();
    const message = question.trim();
    const id = thread.id || crypto.randomUUID();
    const answerId = crypto.randomUUID();
    const questionId = crypto.randomUUID();
    const controller = new AbortController();
    activeRequest.current = controller;
    sessionStorage.setItem(THREAD_KEY, id);
    setQuestion("");
    setError("");
    setNotice("");
    setBusy("Researching…");
    setThread((t) => ({
      ...t,
      id,
      messages: [
        ...t.messages,
        { id: questionId, role: "user", content: message },
        { id: answerId, role: "assistant", content: "" },
      ],
    }));
    try {
      // The stream carries stage events and one complete response, never partial answer text.
      await api.ask(
        id,
        message,
        (event) => {
          if (event.request_id) {
            setThread((t) => ({
              ...t,
              messages: t.messages.map((m) =>
                m.id === questionId
                  ? {
                      ...m,
                      request_id: event.request_id,
                      ...(typeof event.sanitized_message === "string"
                        ? { content: event.sanitized_message }
                        : {}),
                      ...(event.access_scope
                        ? { access_scope: event.access_scope }
                        : {}),
                    }
                  : m,
              ),
            }));
          }
          if (event.type === "node_start") {
            setBusy(event.label || "Researching…");
          } else if (event.type === "final") {
            setThread((t) => ({
              ...t,
              messages: t.messages.map((m) =>
                m.id === answerId
                  ? {
                      ...m,
                      content: event.response,
                      sources: event.sources,
                      confidence: event.confidence,
                    }
                  : m,
              ),
            }));
          } else if (event.type === "pending_approval") {
            setThread((t) => ({
              ...t,
              pending: true,
              approval: event,
              messages: t.messages.filter((m) => m.id !== answerId),
            }));
          } else if (event.type === "error") {
            const error = new Error(event.message);
            error.status = event.status;
            throw error;
          }
        },
        controller.signal,
      );
    } catch (e) {
      if (!controller.signal.aborted) {
        const status = e.response?.status || e.status;
        // Refused before any work started: keep the question so it can be edited and sent again.
        const rejected =
          [400, 403, 409, 422, 429].includes(status) && !!e.response;
        const refusedInStream = [400, 403, 409, 422].includes(e.status);
        const keep = rejected || refusedInStream;
        setThread((t) => ({
          ...t,
          messages: t.messages.filter(
            (m) => m.id !== answerId && (!keep || m.id !== questionId),
          ),
        }));
        if (keep) setQuestion(message);
        setError(
          keep
            ? errorMessage(e)
            : `${errorMessage(e)} Check saved history before submitting again.`,
        );
      }
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setBusy("");
        refreshHistory();
      }
    }
  }

  return {
    history,
    historyLoaded,
    thread,
    question,
    setQuestion,
    busy,
    error,
    notice,
    sending: !!activeRequest.current,
    refreshHistory,
    openConversation,
    newConversation,
    deleteConversation,
    submit,
    reset,
  };
}

export function ConversationList({ research, active, onOpen }) {
  const { history, historyLoaded, thread, busy } = research;
  const [deleting, setDeleting] = useState(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  async function confirmDelete() {
    setDeleteBusy(true);
    setDeleteError("");
    try {
      await research.deleteConversation(deleting.thread_id);
      setDeleting(null);
    } catch (e) {
      setDeleteError(errorMessage(e));
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <div className="conversation-section">
      <div className="sidebar-heading">
        <span>Conversations</span>
        <button
          className="icon-button small"
          onClick={research.refreshHistory}
          disabled={!!busy}
          aria-label="Refresh conversation list"
          title="Refresh"
        >
          <Icon name="refresh" size={13} />
        </button>
      </div>
      <div className="conversation-list">
        {history.length ? (
          history.map((item) => {
            const title = item.title || "Untitled conversation";
            return (
              <div
                className={`conversation-row ${active && thread.id === item.thread_id ? "selected" : ""}`}
                key={item.thread_id}
              >
                <button
                  className="conversation-item"
                  onClick={() => onOpen(item.thread_id)}
                  disabled={!!busy}
                  title={title}
                >
                  <span className="conversation-title">{title}</span>
                  {item.is_interrupted && (
                    <span className="pending-dot" title="Awaiting review" />
                  )}
                </button>
                {!item.is_interrupted && (
                  <button
                    className="icon-button small conversation-delete"
                    onClick={() => {
                      setDeleteError("");
                      setDeleting(item);
                    }}
                    disabled={!!busy}
                    aria-label={`Delete conversation: ${title}`}
                    title="Delete"
                  >
                    <Icon name="trash" size={13} />
                  </button>
                )}
              </div>
            );
          })
        ) : (
          <p className="sidebar-empty">
            {historyLoaded ? "No conversations yet." : "Loading…"}
          </p>
        )}
      </div>
      {deleting && (
        <Modal
          title="Delete this conversation?"
          description={deleting.title || "Untitled conversation"}
          onClose={() => !deleteBusy && setDeleting(null)}
          footer={
            <>
              <button
                className="button secondary"
                onClick={() => setDeleting(null)}
                disabled={deleteBusy}
              >
                Cancel
              </button>
              <button
                className="button danger"
                onClick={confirmDelete}
                disabled={deleteBusy}
              >
                {deleteBusy && <Spinner />}
                Delete
              </button>
            </>
          }
        >
          <div className="stack">
            <p className="modal-text">
              Its questions, answers and request details are removed for good.
            </p>
            <Alert>{deleteError}</Alert>
          </div>
        </Modal>
      )}
    </div>
  );
}

export default function ResearchView({ user, research }) {
  const { thread, question, setQuestion, busy, error, notice, sending } =
    research;
  const chatScroll = useRef(null);
  const followLatest = useRef(true);
  const input = useRef(null);

  // Follow the answer only while the reader is near the bottom.
  function rememberScrollPosition() {
    const panel = chatScroll.current;
    if (!panel) return;
    const pageScrolls = getComputedStyle(panel).overflowY === "visible";
    const remaining = pageScrolls
      ? document.documentElement.scrollHeight -
        window.innerHeight -
        window.scrollY
      : panel.scrollHeight - panel.clientHeight - panel.scrollTop;
    followLatest.current = remaining < 80;
  }

  useEffect(() => {
    window.addEventListener("scroll", rememberScrollPosition, {
      passive: true,
    });
    return () => window.removeEventListener("scroll", rememberScrollPosition);
  }, []);

  useEffect(() => {
    followLatest.current = true;
  }, [thread.id]);

  useLayoutEffect(() => {
    const panel = chatScroll.current;
    if (!panel || !followLatest.current) return;
    // An empty conversation shows the welcome from its top, however small the screen.
    if (!thread.messages.length) {
      panel.scrollTop = 0;
      return;
    }
    if (getComputedStyle(panel).overflowY === "visible") {
      window.scrollTo({
        top: document.documentElement.scrollHeight,
        behavior: "instant",
      });
    } else {
      panel.scrollTop = panel.scrollHeight;
    }
  }, [thread.messages, thread.pending, busy, error, notice]);

  // Grow the question box with its content, up to a limit.
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [question]);

  function submit(event) {
    event.preventDefault();
    research.submit(() => {
      followLatest.current = true;
    });
  }

  function pickSuggestion(text) {
    setQuestion(text);
    input.current?.focus();
  }

  return (
    <div className="research">
      <div
        className="chat-scroll"
        ref={chatScroll}
        onScroll={rememberScrollPosition}
      >
        <div className="chat-content">
          {!thread.messages.length && (
            <section className="welcome">
              <h2>What would you like to research?</h2>
              <p>
                Ask about figures in company filings, compare periods, or check
                a calculation. Every answer cites its sources.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map(([icon, title, text]) => (
                  <button
                    key={title}
                    onClick={() => pickSuggestion(text)}
                    disabled={!!busy}
                  >
                    <strong>{title}</strong>
                    <span>{text}</span>
                    <span className="suggestion-icon">
                      <Icon name={icon} size={16} />
                    </span>
                  </button>
                ))}
              </div>
              <div className="corpus-note">
                <span className="chip">
                  <Icon name="database" size={14} />
                  32 companies
                </span>
                <span className="chip">
                  <Icon name="file" size={14} />
                  84 SEC filings
                </span>
                <span className="chip">
                  <Icon name="done" size={14} />
                  10-K, 10-Q and 8-K reports
                </span>
              </div>
            </section>
          )}

          {thread.messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              {message.role === "assistant" && (
                <div className="message-author">
                  <Logo size={22} />
                  <span>Research Assistant</span>
                </div>
              )}
              <div className="message-body">
                {message.access_scope?.escalated && (
                  <span className="badge warning">Extra access requested</span>
                )}
                {message.content ? (
                  message.role === "user" ? (
                    <p className="question-text">{message.content}</p>
                  ) : (
                    <Answer>{message.content}</Answer>
                  )
                ) : (
                  <div className="thinking">
                    <Spinner />
                    {busy || "Preparing response…"}
                  </div>
                )}
                {message.role === "user" && (
                  <RequestDetails requestId={message.request_id} />
                )}
                {message.role === "assistant" && !!message.content && (
                  <>
                    <Sources sources={message.sources} />
                    {message.confidence != null &&
                      message.confidence < 0.75 && (
                        <p className="verification-note">
                          <Icon name="info" size={14} />
                          Verification was incomplete. Read the answer’s
                          qualifications carefully.
                        </p>
                      )}
                  </>
                )}
              </div>
            </article>
          ))}

          {thread.pending && (
            <div className="approval-wait">
              <span className="approval-wait-icon">
                <Icon name="shield" size={18} />
              </span>
              <div>
                <h3>Waiting for review</h3>
                <p>
                  This question asked for access beyond your role, so an
                  administrator reviews the answer before it is released. It
                  will appear here once they decide.
                </p>
                <button
                  className="link-button"
                  onClick={() => research.openConversation(thread.id)}
                  disabled={!!busy}
                >
                  <Icon name="refresh" size={13} /> Refresh status
                </button>
              </div>
            </div>
          )}
          <Alert>{error}</Alert>
          <Alert kind="success">{notice}</Alert>
          {error && thread.id && (
            <button
              className="link-button recover-button"
              onClick={() => research.openConversation(thread.id)}
              disabled={!!busy}
            >
              Reload saved conversation
            </button>
          )}
        </div>
      </div>

      <div className="composer-area">
        <form
          className={`composer ${thread.pending ? "disabled" : ""}`}
          onSubmit={submit}
        >
          <label className="sr-only" htmlFor="question">
            Ask a financial question
          </label>
          <textarea
            id="question"
            ref={input}
            rows={1}
            placeholder={
              thread.pending
                ? "This conversation is waiting for review."
                : "Ask about the financial filings…"
            }
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            disabled={!!busy || thread.pending}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing
              ) {
                e.preventDefault();
                submit(e);
              }
            }}
          />
          <div className="composer-bar">
            <span className="composer-hint">
              {sending ? (
                <>
                  <Spinner size={12} /> {busy}
                </>
              ) : (
                <span className="hint-keys">
                  <kbd>Enter</kbd> to send · <kbd>Shift</kbd> + <kbd>Enter</kbd>{" "}
                  for a new line
                </span>
              )}
            </span>
            <button
              className="send-button"
              type="submit"
              aria-label="Send question"
              disabled={!question.trim() || !!busy || thread.pending}
            >
              <Icon name="send" size={16} />
            </button>
          </div>
        </form>
        <div className="composer-footer">
          <span>
            Answers come only from the loaded filings; coverage varies by
            company and year.
          </span>
          {user.role !== "admin" && !thread.pending && (
            <details className="escalate">
              <summary>Request access outside your role</summary>
              <div className="escalate-body">
                <p>
                  Start the message with{" "}
                  <code>/escalate doc_type=filings confidentiality=public</code>{" "}
                  and put your question on the next line. An administrator must
                  approve the answer before you see it.
                </p>
                <button
                  type="button"
                  className="link-button"
                  disabled={!!busy}
                  onClick={() =>
                    setQuestion(
                      "/escalate doc_type=filings confidentiality=public\nWhat was Microsoft’s total revenue in FY2023?",
                    )
                  }
                >
                  Fill example
                </button>
              </div>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}
