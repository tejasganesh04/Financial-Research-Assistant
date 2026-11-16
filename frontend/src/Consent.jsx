import { useEffect, useState } from "react";
import { api, errorMessage } from "./api.js";
import { Alert, Avatar, Icon, Logo, Spinner, roleName } from "./components.jsx";
import { SignInForm } from "./SignIn.jsx";
import { signOut } from "./session.js";

// Shown when an MCP client such as Claude Code asks to connect (OAuth authorization).
export default function Consent({ requestId, user, onSignedIn, onSignedOut }) {
  const [request, setRequest] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  useEffect(() => {
    if (!requestId) {
      setLoadError(
        "This link is missing its connection request. Start the connection again from your client.",
      );
      return;
    }
    api
      .oauthRequest(requestId)
      .then(setRequest)
      .catch((e) => setLoadError(errorMessage(e)));
  }, [requestId]);

  async function decide(decision) {
    setBusy(decision);
    setError("");
    try {
      const { redirect_url } = await api.oauthDecide(requestId, decision);
      setDone(decision);
      window.location.assign(redirect_url);
    } catch (e) {
      setError(errorMessage(e));
      setBusy("");
    }
  }

  async function switchAccount() {
    await signOut();
    onSignedOut();
  }

  const client = request?.client_name || "An MCP client";

  return (
    <main className="auth-page">
      <div className="auth-card consent-card">
        <div className="consent-logos" aria-hidden="true">
          <span className="client-mark">
            <Icon name="terminal" size={20} />
          </span>
          <span className="consent-link">
            <span />
            <span />
            <span />
          </span>
          <Logo size={40} />
        </div>

        {loadError ? (
          <>
            <div className="auth-heading">
              <h1>Connection request unavailable</h1>
              <p>{loadError}</p>
            </div>
            <p className="auth-footnote">
              In Claude Code, run <code>/mcp</code> and choose{" "}
              <strong>Authenticate</strong> again.
            </p>
          </>
        ) : !request ? (
          <div className="consent-loading">
            <Spinner size={18} />
            Loading the connection request…
          </div>
        ) : done ? (
          <div className="auth-heading">
            <span className="success-mark">
              <Icon name={done === "approve" ? "check" : "x"} size={22} />
            </span>
            <h1>
              {done === "approve"
                ? `${client} is connected`
                : "Connection declined"}
            </h1>
            <p>
              {done === "approve"
                ? `Returning you to ${client}. You can close this tab once it confirms.`
                : `${client} was not given access. You can close this tab.`}
            </p>
          </div>
        ) : (
          <>
            <div className="auth-heading">
              <h1>Connect {client}</h1>
              <p>
                {client} is asking to use your Financial Research account to
                search filings on your behalf.
              </p>
            </div>

            {user ? (
              <>
                <div className="account-chip">
                  <Avatar name={user.name} size={36} />
                  <div>
                    <strong>{user.name}</strong>
                    <span>
                      {user.user_id} · {roleName(user.role)}
                    </span>
                  </div>
                  <button
                    className="link-button"
                    onClick={switchAccount}
                    disabled={!!busy}
                  >
                    Switch account
                  </button>
                </div>

                <ul className="permission-list">
                  {request.scopes.map((s) => (
                    <li key={s.scope}>
                      <Icon name="check" size={15} />
                      {s.description}
                    </li>
                  ))}
                  <li>
                    <Icon name="lock" size={15} />
                    No access to your conversations, approvals or account
                    settings
                  </li>
                  <li>
                    <Icon name="refresh" size={15} />
                    Access renews every {request.access_token_minutes} minutes
                    and stops if your password changes or you disconnect it
                  </li>
                </ul>

                <Alert>{error}</Alert>

                <div className="consent-actions">
                  <button
                    className="button secondary"
                    onClick={() => decide("deny")}
                    disabled={!!busy}
                  >
                    {busy === "deny" && <Spinner />}
                    Cancel
                  </button>
                  <button
                    className="button primary"
                    onClick={() => decide("approve")}
                    disabled={!!busy}
                  >
                    {busy === "approve" && <Spinner />}
                    Allow access
                  </button>
                </div>
                <p className="auth-footnote">
                  {request.redirect_is_local
                    ? "After you allow access, you'll return to the app on this computer."
                    : `After you allow access, you'll be sent to ${request.redirect_host}.`}{" "}
                  You can disconnect it anytime under Account → Sessions.
                </p>
              </>
            ) : (
              <>
                <p className="consent-signin-note">Sign in to continue.</p>
                <SignInForm
                  onSignedIn={onSignedIn}
                  submitLabel="Sign in and continue"
                />
              </>
            )}
          </>
        )}
      </div>
      <footer className="auth-legal">Financial Research Assistant</footer>
    </main>
  );
}
