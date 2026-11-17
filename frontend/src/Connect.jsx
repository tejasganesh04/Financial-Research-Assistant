import { useEffect, useState } from "react";
import { api } from "./api.js";
import { Alert, CopyField, Icon, PageHeader, roleName } from "./components.jsx";
import { linkHandler } from "./router.js";

export default function Connect({ user }) {
  // The server's configured address: OAuth tokens are issued for that exact URL.
  const [server, setServer] = useState({
    enabled: true,
    mcp_url: `${window.location.origin}/mcp`,
  });
  useEffect(() => {
    api
      .mcpServer()
      .then(setServer)
      .catch(() => {});
  }, []);
  const url = server.mcp_url;
  const command = `claude mcp add --transport http financial-research ${url}`;
  return (
    <div className="page narrow">
      <PageHeader
        title="Connect Claude Code"
        description="Search the same filings from your own AI assistant, with your account and your role’s access."
      />

      {!server.enabled && (
        <Alert kind="warning">
          MCP connections are turned off on this server.
        </Alert>
      )}
      <section className="panel section-panel">
        <ol className="steps">
          <li>
            <span className="step-number">1</span>
            <div>
              <h2>Add the server</h2>
              <p>Run this once in a terminal:</p>
              <CopyField value={command} label="Copy command" />
            </div>
          </li>
          <li>
            <span className="step-number">2</span>
            <div>
              <h2>Sign in</h2>
              <p>
                In Claude Code, type <code>/mcp</code>, choose{" "}
                <strong>financial-research</strong> and select{" "}
                <strong>Authenticate</strong>. A browser tab opens on this site:
                sign in if asked, then select <strong>Allow access</strong>.
              </p>
            </div>
          </li>
          <li>
            <span className="step-number">3</span>
            <div>
              <h2>Ask a question</h2>
              <p>For example:</p>
              <CopyField
                mono={false}
                value="Using financial-research, what was Amazon's FY2017 days payable outstanding? Show the calculation and cite pages."
                label="Copy example question"
              />
            </div>
          </li>
        </ol>
      </section>

      <section className="panel section-panel">
        <div className="section-title">
          <h2>What the connection can do</h2>
        </div>
        <ul className="fact-list">
          <li>
            <Icon name="file" size={16} />
            <div>
              <strong>List, search and read filings</strong>
              <span>
                Three tools: list_documents, search_documents and read_pages.
                Results include the document and page to cite.
              </span>
            </div>
          </li>
          <li>
            <Icon name="shield" size={16} />
            <div>
              <strong>Your access, nothing more</strong>
              <span>
                It sees only what the {roleName(user.role)} role allows, and
                cannot read your conversations, approvals or account.
              </span>
            </div>
          </li>
          <li>
            <Icon name="refresh" size={16} />
            <div>
              <strong>Stays signed in, safely</strong>
              <span>
                Claude Code renews its access every hour on its own. It stops
                working if your password changes, an administrator signs you
                out, or you disconnect it.
              </span>
            </div>
          </li>
          <li>
            <Icon name="monitor" size={16} />
            <div>
              <strong>Manage connections</strong>
              <span>
                See and disconnect connected clients under{" "}
                <a href="/account" onClick={linkHandler("/account")}>
                  Account → Sessions
                </a>
                .
              </span>
            </div>
          </li>
        </ul>
      </section>

      <section className="panel section-panel">
        <div className="section-title">
          <h2>Other MCP clients</h2>
          <p>
            Any client that supports remote MCP servers with OAuth can connect
            to <code>{url}</code>. It discovers the sign-in page by itself; you
            only need the address.
          </p>
        </div>
        <CopyField value={url} label="Copy server address" />
      </section>
    </div>
  );
}
