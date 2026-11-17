import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { withCitations } from "./components.jsx";

// Loaded only when an answer is shown, so the sign-in page does not download the Markdown parser.
export default function AnswerMarkdown({ children }) {
  return (
    <div className="answer">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          img: () => null,
          a: ({ children, href, title }) =>
            href === "#cite" ? (
              <span className="cite" title={title}>
                {children}
              </span>
            ) : (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            ),
          table: ({ children }) => (
            // Focusable so a wide table can be scrolled sideways from the keyboard.
            <div
              className="table-scroll"
              tabIndex={0}
              role="region"
              aria-label="Table"
            >
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {withCitations(children || "")}
      </Markdown>
    </div>
  );
}
