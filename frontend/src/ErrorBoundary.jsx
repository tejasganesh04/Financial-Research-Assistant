import { Component } from "react";
import { Logo } from "./components.jsx";

// Shown instead of a blank page if the interface hits an unexpected error.
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error) {
    console.error(error);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="auth-page">
        <div className="auth-card">
          <div className="auth-heading">
            <Logo size={40} />
            <h1>Something went wrong</h1>
            <p>
              The page hit an unexpected error. Your saved conversations are
              safe. Reload to continue.
            </p>
          </div>
          <button
            className="button primary block"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      </main>
    );
  }
}
