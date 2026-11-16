import { useState } from "react";
import { errorMessage } from "./api.js";
import { Alert, Icon, Logo, Spinner } from "./components.jsx";
import { signIn } from "./session.js";

export function signInError(error) {
  const status = error.response?.status;
  if (status === 401) return "Incorrect username or password.";
  if (status === 403 || status === 429) return errorMessage(error);
  if (status === 503)
    return "Sign-in is temporarily unavailable. Try again in a moment.";
  if (!error.response)
    return "Could not reach the server. Check your connection and try again.";
  return "Unable to sign in right now. Try again in a moment.";
}

export function SignInForm({
  onSignedIn,
  submitLabel = "Sign in",
  autoFocus = true,
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const user = await signIn(username.trim(), password);
      setPassword("");
      onSignedIn(user);
    } catch (e) {
      setError(signInError(e));
      setBusy(false);
    }
  }

  return (
    <form className="auth-form" onSubmit={submit} noValidate={false}>
      <Alert>{error}</Alert>
      <div className="field">
        <label htmlFor="username">Username</label>
        <input
          id="username"
          name="username"
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
          disabled={busy}
          autoFocus={autoFocus}
        />
      </div>
      <div className="field">
        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          disabled={busy}
        />
      </div>
      <details className="forgot">
        <summary>Forgot your password?</summary>
        <p>
          Ask your administrator to reset it. For security, passwords can’t be
          recovered by email.
        </p>
      </details>
      <button className="button primary block" disabled={busy}>
        {busy ? <Spinner /> : null}
        {busy ? "Signing in…" : submitLabel}
      </button>
    </form>
  );
}

export default function SignIn({ onSignedIn, notice }) {
  return (
    <main className="auth-page">
      <div className="auth-card">
        <div className="auth-heading">
          <Logo size={40} />
          <h1>Sign in to Financial Research</h1>
          <p>Cited answers from company filings, for invited members.</p>
        </div>
        <Alert kind="info">{notice}</Alert>
        <SignInForm onSignedIn={onSignedIn} />
        <p className="auth-footnote">
          <Icon name="lock" size={13} />
          Accounts are created by your administrator. There is no public
          sign-up.
        </p>
      </div>
      <footer className="auth-legal">Financial Research Assistant</footer>
    </main>
  );
}
