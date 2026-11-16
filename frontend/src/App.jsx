import { useEffect, useState } from "react";
import Account from "./Account.jsx";
import AdminActivity from "./AdminActivity.jsx";
import AdminUsers from "./AdminUsers.jsx";
import { api } from "./api.js";
import Approvals from "./Approvals.jsx";
import {
  Avatar,
  Badge,
  Brand,
  Icon,
  Logo,
  Spinner,
  roleName,
} from "./components.jsx";
import Connect from "./Connect.jsx";
import Consent from "./Consent.jsx";
import ResearchView, { ConversationList, useResearch } from "./Research.jsx";
import { linkHandler, navigate, useLocation } from "./router.js";
import {
  forgetSession,
  onSignedOutElsewhere,
  renewSession,
  signOut,
} from "./session.js";
import SignIn from "./SignIn.jsx";

const ADMIN_PATHS = ["/approvals", "/admin/users", "/admin/activity"];
const KNOWN_PATHS = ["/", "/account", "/connect", ...ADMIN_PATHS];

export default function App() {
  const { path, query } = useLocation();
  const [user, setUser] = useState(null);
  const [booting, setBooting] = useState(true);
  const [notice, setNotice] = useState("");

  // The access token is only in memory, so every page load starts by renewing it from the cookie.
  useEffect(() => {
    renewSession()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setBooting(false));
    const expired = () => {
      forgetSession();
      setUser(null);
      setNotice("Your session has ended. Sign in again to continue.");
    };
    window.addEventListener("session-expired", expired);
    const stopListening = onSignedOutElsewhere(() => {
      forgetSession();
      setUser(null);
      setNotice("You signed out in another tab.");
    });
    return () => {
      window.removeEventListener("session-expired", expired);
      stopListening();
    };
  }, []);

  function signedIn(next) {
    setNotice("");
    setUser(next);
    if (path === "/sign-in") navigate("/", { replace: true });
  }

  if (booting)
    return (
      <div className="boot-screen" aria-busy="true">
        <Logo size={36} />
        <Spinner size={16} />
      </div>
    );

  if (path === "/oauth/consent")
    return (
      <Consent
        requestId={query.get("request")}
        user={user}
        onSignedIn={signedIn}
        onSignedOut={() => setUser(null)}
      />
    );

  if (!user) return <SignIn onSignedIn={signedIn} notice={notice} />;

  return (
    <Shell
      user={user}
      path={path}
      onSignOut={async () => {
        await signOut();
        setUser(null);
        setNotice("");
        navigate("/", { replace: true });
      }}
    />
  );
}

function Shell({ user, path, onSignOut }) {
  const research = useResearch(user);
  const [menuOpen, setMenuOpen] = useState(false);
  const [pending, setPending] = useState(0);
  const admin = user.role === "admin";

  let view = KNOWN_PATHS.includes(path) ? path : "/";
  if (!admin && ADMIN_PATHS.includes(view)) view = "/";
  useEffect(() => {
    if (view !== path) navigate(view, { replace: true });
  }, [view, path]);

  useEffect(() => {
    if (admin)
      api
        .approvals()
        .then((items) => setPending(items.length))
        .catch(() => {});
  }, [admin]);

  useEffect(() => setMenuOpen(false), [path, research.thread.id]);

  async function signOutEverywhereHere() {
    research.reset();
    await onSignOut();
  }

  const go = (to) => {
    setMenuOpen(false);
    navigate(to);
  };

  const navItem = (to, icon, label, extra) => (
    <a
      href={to}
      className={`nav-item ${view === to ? "active" : ""}`}
      aria-current={view === to ? "page" : undefined}
      onClick={linkHandler(to)}
    >
      <Icon name={icon} size={16} />
      <span>{label}</span>
      {extra}
    </a>
  );

  const title = {
    "/approvals": "Approvals",
    "/admin/users": "Users",
    "/admin/activity": "Activity",
    "/account": "Account",
    "/connect": "Connect Claude Code",
  }[view];
  const threadTitle =
    research.history.find((item) => item.thread_id === research.thread.id)
      ?.title ||
    (research.thread.messages.length ? "Conversation" : "New research");

  return (
    <div className={`app ${menuOpen ? "menu-open" : ""}`}>
      <aside className="sidebar" aria-label="Navigation">
        <div className="sidebar-top">
          <a href="/" className="brand-link" onClick={linkHandler("/")}>
            <Brand />
          </a>
          <button
            className="icon-button sidebar-close"
            onClick={() => setMenuOpen(false)}
            aria-label="Close menu"
          >
            <Icon name="x" />
          </button>
        </div>
        <button
          className="button new-conversation"
          onClick={() => {
            research.newConversation();
            go("/");
          }}
          disabled={!!research.busy}
        >
          <Icon name="plus" size={15} />
          New conversation
        </button>
        <nav className="nav" aria-label="Workspace">
          {navItem("/", "chat", "Research")}
          {admin &&
            navItem(
              "/approvals",
              "shield",
              "Approvals",
              pending > 0 ? <span className="nav-count">{pending}</span> : null,
            )}
          {admin && (
            <>
              <div className="nav-label">Admin</div>
              {navItem("/admin/users", "users", "Users")}
              {navItem("/admin/activity", "activity", "Activity")}
            </>
          )}
        </nav>
        <ConversationList
          research={research}
          active={view === "/"}
          onOpen={(id) => {
            research.openConversation(id);
            go("/");
          }}
        />
        <div className="sidebar-footer">
          {navItem("/connect", "terminal", "Connect Claude Code")}
          <div className="profile">
            <a
              href="/account"
              className="profile-link"
              onClick={linkHandler("/account")}
              title="Account settings"
            >
              <Avatar name={user.name} size={30} />
              <span className="profile-text">
                <strong>{user.name}</strong>
                <small>{roleName(user.role)}</small>
              </span>
            </a>
            <button
              className="icon-button"
              onClick={signOutEverywhereHere}
              disabled={research.sending}
              aria-label="Sign out"
              title="Sign out"
            >
              <Icon name="logout" size={16} />
            </button>
          </div>
        </div>
      </aside>
      <div className="sidebar-scrim" onClick={() => setMenuOpen(false)} />

      <div className="main">
        <header className="topbar">
          <button
            className="icon-button menu-button"
            onClick={() => setMenuOpen(true)}
            aria-label="Open menu"
          >
            <Icon name="menu" />
          </button>
          {/* Pages have their own h1; the research view uses the conversation title. */}
          {view === "/" ? (
            <h1 className="topbar-title">{threadTitle}</h1>
          ) : (
            <div className="topbar-title">{title}</div>
          )}
          <Badge tone="neutral">
            <Icon name="shield" size={12} />
            {roleName(user.role)}
          </Badge>
        </header>
        <main className="content">
          {/* The research view stays mounted so a running question survives moving between pages. */}
          <div className="view" hidden={view !== "/"}>
            <ResearchView user={user} research={research} />
          </div>
          {view === "/approvals" && <Approvals onCountChange={setPending} />}
          {view === "/admin/users" && <AdminUsers me={user} />}
          {view === "/admin/activity" && <AdminActivity />}
          {view === "/account" && <Account user={user} />}
          {view === "/connect" && <Connect user={user} />}
        </main>
      </div>
    </div>
  );
}
