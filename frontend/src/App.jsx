import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errorMessage, TOKEN_KEY } from "./api.js";
import {
  Alert,
  Answer,
  Brand,
  Icon,
  Login,
  Sources,
  RequestDetails,
  roleName,
} from "./components.jsx";
import Approvals from "./Approvals.jsx";

// The selected conversation ID survives a refresh in this browser tab. Messages are loaded from PostgreSQL.
const THREAD_KEY = "financial-research-conversation";
const emptyConversation = () => ({ id: null, messages: [], pending: false });
// Convert saved backend messages into the shape used by the conversation screen.
const fromHistory = (data) => ({
  id: data.thread_id,
  pending: data.is_interrupted,
  approval: data.interrupt_payload,
  messages: data.messages.map((m, i) => ({
    ...m,
    id: `${data.thread_id}-${i}`,
  })),
});
const suggestions = [
  [
    "Find a figure",
    "What was Microsoft’s cost of goods sold in FY2016? Give the answer in USD millions.",
  ],
  [
    "Compare years",
    "Compare Microsoft’s total revenue in FY2016 and FY2023. Cite each year’s filing.",
  ],
  [
    "Research a metric",
    "How do Microsoft’s FY2016 revenue and cost of revenue reconcile to gross profit? Show the calculation.",
  ],
];

// FLOW: restore/login -> load sidebar -> select/new conversation -> send question -> show stages and result.
export default function App() {
  // React state controls the screen. The backend independently checks the signed JWT and permissions.
  const [user, setUser] = useState(null);
  const [booting, setBooting] = useState(true);
  const [tab, setTab] = useState("research");
  const [history, setHistory] = useState([]);
  const [thread, setThread] = useState(emptyConversation);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const activeRequest = useRef(null);
  const historyVersion = useRef(0);
  const chatScroll = useRef(null);
  const followLatest = useRef(true);

  // LOGOUT: Clear this tab's token, selected conversation and screen. Saved PostgreSQL conversations remain.
  function logout() {
    activeRequest.current?.abort();
    activeRequest.current = null;
    historyVersion.current += 1;
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(THREAD_KEY);
    setUser(null);
    setHistory([]);
    setThread(emptyConversation());
    setQuestion("");
    setBusy("");
    setError("");
    setNotice("");
    setTab("research");
  }

  // PAGE REFRESH: Validate the stored token through /auth/me before restoring the logged-in screen.
  useEffect(() => {
    let mounted = true;
    const expire = () => logout();
    window.addEventListener("session-expired", expire);
    if (sessionStorage.getItem(TOKEN_KEY)) {
      api
        .me()
        .then((me) => {
          if (mounted) setUser(me);
        })
        .catch(() => {
          if (mounted) logout();
        })
        .finally(() => {
          if (mounted) setBooting(false);
        });
    } else setBooting(false);
    return () => {
      mounted = false;
      window.removeEventListener("session-expired", expire);
    };
  }, []);

  // LOGIN 1: POST credentials, store the returned JWT, GET /auth/me, then populate React user state.
  async function login(username, password) {
    const response = await api.login(username, password);
    sessionStorage.setItem(TOKEN_KEY, response.access_token);
    try {
      const me = await api.me();
      setError("");
      setNotice("");
      setUser(me);
    } catch (e) {
      sessionStorage.removeItem(TOKEN_KEY);
      throw e;
    }
  }

  // LOGIN 2: GET /conversations for this token's user; put the returned list in the sidebar.
  async function refreshHistory() {
    const token = sessionStorage.getItem(TOKEN_KEY);
    try {
      const conversations = await api.conversations();
      if (token === sessionStorage.getItem(TOKEN_KEY))
        setHistory(conversations);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  // OPEN HISTORY: GET this conversation's saved messages/status, display them, and remember its ID in this tab.
  async function openConversation(id) {
    if (activeRequest.current) return;
    const version = ++historyVersion.current;
    setBusy("Opening conversation…");
    setError("");
    setNotice("");
    try {
      const data = await api.conversation(id);
      if (version !== historyVersion.current) return;
      followLatest.current = true;
      setThread(fromHistory(data));
      setQuestion("");
      setTab("research");
      // Remember which saved conversation this tab should reopen on refresh.
      sessionStorage.setItem(THREAD_KEY, id);
    } catch (e) {
      if (version === historyVersion.current) setError(errorMessage(e));
    } finally {
      if (version === historyVersion.current) setBusy("");
    }
  }

  // LOGIN 3: After user state is set, load the sidebar and reopen the previously selected conversation, if any.
  useEffect(() => {
    if (!user) return;
    refreshHistory();
    const saved = sessionStorage.getItem(THREAD_KEY);
    if (saved) openConversation(saved);
  }, [user?.user_id]);

  // Only pending approvals are polled. These reads never start another answer.
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
      } catch (e) {
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

  // Follow the answer only while the reader is near the bottom. Instant
  // movement avoids competing smooth animations during answer updates.
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

  useLayoutEffect(() => {
    const panel = chatScroll.current;
    if (!panel || !followLatest.current) return;
    if (getComputedStyle(panel).overflowY === "visible") {
      window.scrollTo({
        top: document.documentElement.scrollHeight,
        behavior: "instant",
      });
    } else {
      panel.scrollTop = panel.scrollHeight;
    }
  }, [thread.messages, thread.pending, busy, error, notice, tab]);

  // NEW CONVERSATION: Clear only the view and selected ID. No UUID, API request or database row is created yet.
  function newConversation() {
    if (activeRequest.current) return;
    historyVersion.current += 1;
    setBusy("");
    setThread(emptyConversation());
    setQuestion("");
    setError("");
    setNotice("");
    setTab("research");
    sessionStorage.removeItem(THREAD_KEY);
  }

  // SEND: Reuse/create the conversation ID, show a waiting message, then send one authenticated question request.
  async function submit(event) {
    event.preventDefault();
    if (!question.trim() || activeRequest.current || busy || thread.pending)
      return;
    followLatest.current = true;
    const message = question.trim();
    // Create a random conversation UUID on its first question; reuse it on follow-ups. It does not encode the user.
    const id = thread.id || crypto.randomUUID();
    // These two IDs identify temporary React message elements, not conversations or backend request traces.
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
      // SSE: Listen for stage events and one complete response. The backend does not forward answer tokens.
      await api.ask(
        id,
        message,
        (event) => {
          // The backend-generated request ID links this question to logs/traces/<id>/summary.md.
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
          // STAGE: Change the waiting label. FINAL: fill in the answer. PENDING: hide the answer and wait for Admin.
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
        const rejected = [400, 403, 409, 422].includes(e.response?.status || e.status);
        setThread((t) => ({
          ...t,
          messages: t.messages.filter(
            (m) => m.id !== answerId && (!rejected || m.id !== questionId),
          ),
        }));
        if (rejected) setQuestion(message);
        setError(rejected ? errorMessage(e) : `${errorMessage(e)} Check saved history before submitting again.`);
      }
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setBusy("");
        // After the request ends, reload the sidebar so newly checkpointed conversations appear there.
        refreshHistory();
      }
    }
  }

  if (booting)
    return (
      <div className="boot-screen">
        <Brand />
        <p>Opening workspace…</p>
      </div>
    );
  if (!user) return <Login onLogin={login} />;
  const reviewer = user.role === "admin";
  const isSending = !!activeRequest.current;

  return (
    <div className="workspace">
      <aside className="sidebar">
        <Brand />
        <button
          className="new-conversation"
          onClick={newConversation}
          disabled={!!busy}
        >
          <Icon name="plus" />
          New conversation
        </button>
        <nav aria-label="Workspace">
          <button
            className={`nav-item ${tab === "research" ? "active" : ""}`}
            onClick={() => setTab("research")}
            disabled={!!busy}
          >
            <Icon name="chat" />
            Research
          </button>
          {reviewer && (
            <button
              className={`nav-item ${tab === "approvals" ? "active" : ""}`}
              onClick={() => setTab("approvals")}
              disabled={!!busy}
            >
              <Icon name="shield" />
              Approvals
            </button>
          )}
        </nav>
        <div className="history-heading">
          <span>CONVERSATIONS</span>
          <button
            className="icon-button"
            onClick={refreshHistory}
            disabled={!!busy}
            aria-label="Refresh conversation list"
          >
            <Icon name="refresh" size={14} />
          </button>
        </div>
        <div className="history-list">
          {history.length ? (
            history.map((item) => (
              <button
                className={`history-item ${thread.id === item.thread_id && tab === "research" ? "selected" : ""}`}
                key={item.thread_id}
                onClick={() => openConversation(item.thread_id)}
                disabled={!!busy}
              >
                <Icon name="chat" size={15} />
                <span>{item.title || "Untitled conversation"}</span>
                {item.is_interrupted && (
                  <span className="pending-dot" title="Awaiting review" />
                )}
              </button>
            ))
          ) : (
            <p className="history-empty">
              Your conversations will appear here.
            </p>
          )}
        </div>
        <div className="profile">
          <span className="avatar">{roleName(user.role).slice(0, 1)}</span>
          <div>
            <strong>{roleName(user.role)}</strong>
            <small>{user.department}</small>
          </div>
          <button
            className="icon-button"
            onClick={logout}
            disabled={!!busy}
            aria-label="Sign out"
          >
            <Icon name="logout" />
          </button>
        </div>
      </aside>
      <main className="main-panel">
        <header className="workspace-header">
          <div>
            <span className="section-label">FINANCIAL RESEARCH ASSISTANT</span>
            <h1>
              {tab === "research" ? "Research workspace" : "Review requests"}
            </h1>
          </div>
          <span className="role-badge">
            <Icon name="shield" size={14} />
            {roleName(user.role)}
          </span>
        </header>
        {tab === "approvals" ? (
          <Approvals />
        ) : (
          <>
            <div
              className="chat-scroll"
              ref={chatScroll}
              onScroll={rememberScrollPosition}
            >
              <div className="chat-content">
                {!thread.messages.length && (
                  <section className="welcome">
                    <div className="welcome-icon">
                      <Icon name="chart" size={30} />
                    </div>
                    <span className="section-label">
                      FROM QUESTION TO EVIDENCE
                    </span>
                    <h2>What would you like to research?</h2>
                    <p>
                      Ask about financial figures, compare reporting periods,
                      <br className="desktop-break" /> or explore a company’s
                      filings. Answers include source references.
                    </p>
                    <div className="suggestions">
                      {suggestions.map(([title, text]) => (
                        <button
                          key={title}
                          onClick={() => setQuestion(text)}
                          disabled={!!busy}
                        >
                          <span>
                            {title}
                            <Icon name="arrow" size={15} />
                          </span>
                          <p>{text}</p>
                        </button>
                      ))}
                    </div>
                    <div className="corpus-note">
                      <span className="status-dot" />
                      32 companies<span>·</span>84 filings<span>·</span>
                      Published knowledge base
                    </div>
                  </section>
                )}
                {thread.messages.map((message) => (
                  <article
                    className={`message ${message.role}`}
                    key={message.id}
                  >
                    <div className="message-heading">
                      <span
                        className={`message-avatar ${message.role === "user" ? "user-avatar" : ""}`}
                      >
                        <Icon
                          name={message.role === "user" ? "chat" : "chart"}
                          size={16}
                        />
                      </span>
                      <strong>
                        {message.role === "user" ? "You" : "Research Assistant"}
                      </strong>
                      {message.access_scope?.escalated && (
                        <span className="pill amber">
                          Extra access requested
                        </span>
                      )}
                    </div>
                    <div className="message-body">
                      {message.content ? (
                        <Answer>{message.content}</Answer>
                      ) : (
                        <div className="thinking">
                          <span className="spinner" />
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
                              <span className="verification-note">
                                Verification incomplete — read the answer’s
                                qualification carefully.
                              </span>
                            )}
                        </>
                      )}
                    </div>
                  </article>
                ))}
                {thread.pending && (
                  <div className="approval-wait">
                    <span className="wait-icon">
                      <Icon name="shield" size={23} />
                    </span>
                    <div>
                      <h3>Waiting for review</h3>
                      <p>
                        Your question requested extra access. The draft is held
                        until an Admin reviews it. Your answer will appear here
                        after a decision.
                      </p>
                      <button
                        className="text-button"
                        onClick={() => openConversation(thread.id)}
                        disabled={!!busy}
                      >
                        Refresh status <Icon name="refresh" size={13} />
                      </button>
                    </div>
                  </div>
                )}
                <Alert>{error}</Alert>
                <Alert kind="success">{notice}</Alert>
              </div>
            </div>
            <div className="composer-area">
              <form className="composer" onSubmit={submit}>
                <label className="sr-only" htmlFor="question">
                  Ask a financial question
                </label>
                <textarea
                  id="question"
                  rows={2}
                  placeholder={
                    thread.pending
                      ? "This conversation is waiting for review."
                      : "Ask a question about the available financial documents…"
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
                <div className="composer-bottom">
                  <span>
                    {isSending
                      ? busy
                      : "Enter to send · Shift + Enter for a new line"}
                  </span>
                  <button
                    className="send-button"
                    type="submit"
                    aria-label="Send question"
                    disabled={!question.trim() || !!busy || thread.pending}
                  >
                    <Icon name="send" size={17} />
                  </button>
                </div>
              </form>
              {user.role !== "admin" && !thread.pending && (
                <details className="composer-note">
                  <summary>Request access outside your role</summary>
                  <p>
                    Start with{" "}
                    <code>
                      /escalate doc_type=filings confidentiality=public
                    </code>
                    , then put your question on a new line. Admin must approve
                    the answer. Use Shift + Enter for a new line.
                  </p>
                  <button
                    type="button"
                    className="text-button"
                    disabled={!!busy}
                    onClick={() =>
                      setQuestion(
                        "/escalate doc_type=filings confidentiality=public\nWhat was Microsoft’s total revenue in FY2023?",
                      )
                    }
                  >
                    Fill example
                  </button>
                </details>
              )}
              <p className="composer-note">
                Answers use the loaded documents. Coverage varies by company and
                year.
              </p>
              {error && thread.id && (
                <button
                  className="text-button recover-button"
                  onClick={() => openConversation(thread.id)}
                  disabled={!!busy}
                >
                  Reload saved conversation
                </button>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
