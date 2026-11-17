import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api.js";
import {
  Alert,
  Icon,
  PageHeader,
  Spinner,
  formatDateTime,
  timeAgo,
} from "./components.jsx";

// The admin Health page: is everything up, how is the assistant used, and how is the AI doing.
// Every number comes from GET /v1/admin/health; nothing here calls a model.

const RANGES = [
  ["24h", "24 hours", "previous 24 hours"],
  ["7d", "7 days", "previous 7 days"],
  ["30d", "30 days", "previous 30 days"],
];
const REFRESH_MS = 60_000;

// Outcome order keeps amber and red apart (checked for colour-blind separation in both themes).
const OUTCOMES = [
  ["answered", "Answered", ["answered"]],
  ["review", "Held for review", ["awaiting_approval"]],
  ["blocked", "Blocked by safety checks", ["blocked"]],
  [
    "declined",
    "Not answered",
    ["no_evidence", "out_of_scope", "clarification"],
  ],
  ["errors", "Errors", ["failed", "rate_limited", "unknown"]],
  ["stopped", "Stopped before the end", ["interrupted", "cancelled"]],
];

const STATE_ICON = { ok: "done", warn: "alert", down: "error", off: "info" };
const STATE_WORD = {
  ok: "Working",
  warn: "Needs attention",
  down: "Down",
  off: "Not monitored",
};

const whole = new Intl.NumberFormat();
const compactFormat = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});
const compact = (n) =>
  n < 10_000 ? whole.format(Math.round(n)) : compactFormat.format(n);
const percent = (x, digits = 0) => `${(x * 100).toFixed(digits)}%`;
const money = (x) =>
  `$${x.toFixed(x > 0 && x < 0.01 ? 4 : x > 0 && x < 0.1 ? 3 : 2)}`;

function duration(ms) {
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  const minutes = Math.floor(s / 60);
  const rest = Math.round(s % 60);
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

const timeZone = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

// Buckets arrive as wall-clock times in the viewer's zone ("2026-11-18T14:00:00").
function bucketDate(t) {
  const [day, time = "00:00:00"] = t.split("T");
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d, Number(time.split(":")[0]));
}
const hourLabel = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
});
const dayLabel = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  day: "numeric",
  month: "short",
});
function bucketLabel(t, range) {
  const date = bucketDate(t);
  if (range !== "24h") return dayLabel.format(date);
  const today = new Date();
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const day = sameDay(date, today)
    ? "Today"
    : sameDay(date, yesterday)
      ? "Yesterday"
      : dayLabel.format(date);
  return `${day} ${hourLabel.format(date)}`;
}

const reducedMotion = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// Numbers ease to their new value; people who prefer less motion see them change at once.
function AnimatedNumber({ value, format }) {
  const [shown, setShown] = useState(reducedMotion() ? value : 0);
  const from = useRef(shown);
  useEffect(() => {
    if (reducedMotion()) {
      setShown(value);
      return undefined;
    }
    const start = performance.now();
    const begin = from.current;
    let frame;
    const tick = (now) => {
      const p = Math.min(1, (now - start) / 700);
      const eased = 1 - Math.pow(1 - p, 3);
      const next = begin + (value - begin) * eased;
      setShown(next);
      from.current = next;
      if (p < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value]);
  return <>{format(shown)}</>;
}

function useWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    setWidth(element.clientWidth);
    const observer = new ResizeObserver(([entry]) =>
      setWidth(Math.floor(entry.contentRect.width)),
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

// A small trend under a figure: columns for counts, a line for times. Hover or the arrow keys
// show each point; a hidden table carries the same values for screen readers.
function Trend({ points, kind, label, format, range }) {
  const [ref, width] = useWidth();
  const [active, setActive] = useState(null);
  const height = 44;
  const values = points.map((p) => p.value);
  const present = values.filter((v) => v != null);
  const max = Math.max(...present, 0) || 1;
  const n = points.length;
  const step = n > 1 ? width / n : width;
  const x = (i) => step * i + step / 2;
  const y = (v) => height - 4 - (v / max) * (height - 10);

  function pick(event) {
    const box = ref.current.getBoundingClientRect();
    const i = Math.floor(((event.clientX - box.left) / box.width) * n);
    setActive(Math.max(0, Math.min(n - 1, i)));
  }
  function keys(event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.key === "ArrowLeft" ? -1 : 1;
    setActive((i) => Math.max(0, Math.min(n - 1, (i ?? n - 1) + delta)));
  }

  let marks = null;
  if (width > 0 && kind === "bars") {
    const gap = 2;
    const bar = Math.max(1, Math.min(24, step - gap));
    marks = points.map((p, i) => {
      const v = p.value || 0;
      const h = v ? Math.max(3, (v / max) * (height - 6)) : 0;
      const left = x(i) - bar / 2;
      const r = Math.min(3, bar / 2, h);
      const top = height - h;
      return (
        <g key={p.t}>
          <rect
            x={left}
            y={height - 1}
            width={bar}
            height={1}
            className="trend-baseline"
          />
          {h > 0 && (
            <path
              className={
                i === active || (active == null && i === n - 1)
                  ? "trend-bar current"
                  : "trend-bar"
              }
              d={`M${left},${height} V${top + r} Q${left},${top} ${left + r},${top} H${left + bar - r} Q${left + bar},${top} ${left + bar},${top + r} V${height} Z`}
            />
          )}
        </g>
      );
    });
  } else if (width > 0) {
    // One line through the periods that had answers; each of those periods gets a small dot, so
    // a quiet hour between two busy ones is visible rather than implied.
    const known = points
      .map((p, i) => (p.value == null ? null : [x(i), y(p.value), i]))
      .filter(Boolean);
    const last = known.length ? known[known.length - 1][2] : -1;
    marks = (
      <>
        {known.length > 1 && (
          <>
            <path
              className="trend-area"
              d={`M${known[0][0]},${height} ${known.map(([a, b]) => `L${a},${b}`).join(" ")} L${known[known.length - 1][0]},${height} Z`}
            />
            <path
              className="trend-line"
              d={known
                .map(([a, b], j) => `${j ? "L" : "M"}${a},${b}`)
                .join(" ")}
            />
          </>
        )}
        {known.map(([a, b, i]) => (
          <circle key={i} className="trend-point" cx={a} cy={b} r={2} />
        ))}
        {active != null && (
          <line
            className="trend-crosshair"
            x1={x(active)}
            x2={x(active)}
            y1={0}
            y2={height}
          />
        )}
        {(active != null ? values[active] != null : last >= 0) && (
          <circle
            className="trend-dot"
            cx={x(active ?? last)}
            cy={y(values[active ?? last])}
            r={4}
          />
        )}
      </>
    );
  }

  const shown = active != null ? points[active] : null;
  return (
    <div
      className="trend"
      ref={ref}
      tabIndex={0}
      role="group"
      aria-label={`${label} by ${range === "24h" ? "hour" : "day"}. Use the arrow keys to read each value.`}
      onPointerMove={pick}
      onPointerLeave={() => setActive(null)}
      onKeyDown={keys}
      onBlur={() => setActive(null)}
    >
      <svg width={width} height={height} aria-hidden="true">
        {marks}
      </svg>
      {shown && (
        <div
          className="trend-tip"
          role="status"
          style={{
            left: Math.max(4, Math.min(width - 4, x(active))),
          }}
        >
          <strong>
            {shown.value == null ? "No answers" : format(shown.value)}
          </strong>
          <span>{bucketLabel(shown.t, range)}</span>
        </div>
      )}
      <table className="sr-only">
        <caption>{label}</caption>
        <tbody>
          {points.map((p) => (
            <tr key={p.t}>
              <th scope="row">{bucketLabel(p.t, range)}</th>
              <td>{p.value == null ? "No answers" : format(p.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Tile({ icon, label, children, footer, trend, tone }) {
  return (
    <section className={`panel health-tile ${tone || ""}`}>
      <h2 className="health-tile-label">
        <Icon name={icon} size={16} />
        {label}
      </h2>
      <div className="health-tile-body">{children}</div>
      {trend}
      {footer && <div className="health-tile-footer">{footer}</div>}
    </section>
  );
}

function Delta({ now, before, period }) {
  if (!now && !before) return null;
  let icon = "flat";
  let text = `Same as the ${period}`;
  if (!before) {
    icon = "up";
    text = `None in the ${period}`;
  } else if (now !== before) {
    const change = (now - before) / before;
    icon = change > 0 ? "up" : "down";
    text = `${change > 0 ? "+" : ""}${Math.round(change * 100)}% vs the ${period}`;
  }
  return (
    <span className="health-delta">
      <Icon name={icon} size={14} />
      {text}
    </span>
  );
}

function Meter({ share }) {
  const tone = share > 0.5 ? "" : share > 0.2 ? "warning" : "danger";
  return (
    <div className={`health-meter ${tone}`} aria-hidden="true">
      <span style={{ width: `${Math.max(0, Math.min(1, share)) * 100}%` }} />
    </div>
  );
}

function StatusPanel({ data }) {
  const { overall, checks, release, langsmith, generated_at } = data;
  return (
    <section
      className={`panel health-status ${overall.state}`}
      aria-labelledby="health-overall"
    >
      <div className="health-status-main">
        <span className="health-status-icon">
          <Icon name={STATE_ICON[overall.state]} size={22} />
        </span>
        <div className="health-status-text">
          <h2 id="health-overall">{overall.message}</h2>
          <p>
            Release <code>{release.version}</code>
            {" · "}Updated{" "}
            {new Date(generated_at).toLocaleTimeString(undefined, {
              timeStyle: "short",
            })}
            {" · "}
            {langsmith.enabled ? (
              langsmith.project_url ? (
                <a
                  href={langsmith.project_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  LangSmith tracing on
                </a>
              ) : (
                "LangSmith tracing on"
              )
            ) : (
              "LangSmith tracing off"
            )}
          </p>
        </div>
      </div>
      <ul className="health-checks" aria-label="Services">
        {checks.map((check) => (
          <li key={check.key} className={`health-check ${check.state}`}>
            <span className="health-check-icon" title={STATE_WORD[check.state]}>
              <Icon name={STATE_ICON[check.state]} size={16} />
              <span className="sr-only">{STATE_WORD[check.state]}: </span>
            </span>
            <span className="health-check-text">
              <strong>{check.label}</strong>
              <span>{check.detail}</span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Tiles({ data, range }) {
  const period = RANGES.find(([value]) => value === range)[2];
  const { questions, latency, groq, ai, mcp, series } = data;
  const trend = (key) => series.map((p) => ({ t: p.t, value: p[key] }));
  return (
    <div className="health-tiles">
      <Tile
        icon="questions"
        label="Questions"
        trend={
          <Trend
            points={trend("questions")}
            kind="bars"
            label="Questions"
            format={(v) => `${whole.format(v)} question${v === 1 ? "" : "s"}`}
            range={range}
          />
        }
        footer={
          questions.people
            ? `From ${questions.people} ${questions.people === 1 ? "person" : "people"}`
            : "Nobody has asked yet"
        }
      >
        <span className="health-figure">
          <AnimatedNumber value={questions.total} format={(v) => compact(v)} />
        </span>
        <Delta
          now={questions.total}
          before={questions.previous}
          period={period}
        />
      </Tile>

      <Tile
        icon="timer"
        label="Answer time"
        trend={
          <Trend
            points={trend("p50_ms")}
            kind="line"
            label="Typical answer time"
            format={duration}
            range={range}
          />
        }
        footer={
          latency.queued
            ? `${latency.queued} waited for a free slot, ${duration(latency.queue_avg_ms || 0)} on average`
            : latency.p95_ms != null
              ? `Slowest 1 in 20: ${duration(latency.p95_ms)}`
              : "Times appear after the first answer"
        }
      >
        {latency.p50_ms != null ? (
          <>
            <span className="health-figure">
              <AnimatedNumber value={latency.p50_ms} format={duration} />
            </span>
            <span className="health-caption">typical</span>
          </>
        ) : (
          <span className="health-figure muted">No answers yet</span>
        )}
      </Tile>

      {groq ? (
        <Tile
          icon="gauge"
          label="Groq allowance today"
          tone={groq.questions_left < 2 ? "attention" : ""}
          trend={<Meter share={groq.share_left ?? 0} />}
          footer={<GroqFooter groq={groq} />}
        >
          <span
            className="health-figure"
            title="Estimated from this server's questions and the limits Groq reports with each reply. Other uses of the same key count only once Groq reports them."
          >
            <AnimatedNumber
              value={groq.share_left ?? 0}
              format={(v) => percent(v)}
            />
          </span>
          <span className="health-caption">
            left · about {groq.questions_left} more question
            {groq.questions_left === 1 ? "" : "s"}
          </span>
        </Tile>
      ) : (
        <Tile
          icon="gauge"
          label="Model spend"
          footer={
            ai.cost_per_question != null
              ? `${money(ai.cost_per_question)} per answered question`
              : "No paid model calls in this period"
          }
        >
          <span className="health-figure">
            <AnimatedNumber value={ai.cost_usd} format={money} />
          </span>
        </Tile>
      )}

      <Tile
        icon="terminal"
        label="Claude Code searches"
        trend={
          <Trend
            points={trend("mcp_calls")}
            kind="bars"
            label="MCP tool calls"
            format={(v) => `${whole.format(v)} call${v === 1 ? "" : "s"}`}
            range={range}
          />
        }
        footer={
          mcp.calls
            ? `${mcp.people} ${mcp.people === 1 ? "person" : "people"}${mcp.search_p50_ms != null ? ` · typical search ${duration(mcp.search_p50_ms)}` : ""}`
            : "No MCP connections used"
        }
      >
        <span className="health-figure">
          <AnimatedNumber value={mcp.calls} format={(v) => compact(v)} />
        </span>
        <span className="health-caption">
          tool call{mcp.calls === 1 ? "" : "s"}
        </span>
      </Tile>
    </div>
  );
}

function GroqFooter({ groq }) {
  const limited = groq.models.find((m) => m.limited_now);
  if (limited)
    return (
      <span className="health-warning-text">
        Rate-limited now
        {limited.retry_after_seconds
          ? `, try again in about ${Math.ceil(limited.retry_after_seconds / 60)} min`
          : ""}
      </span>
    );
  const tightest = groq.models.find((m) => m.model === groq.limiting_model);
  if (tightest?.requests_remaining != null)
    return `${whole.format(tightest.requests_remaining)} of ${whole.format(tightest.requests_limit)} requests left today`;
  return `Limited by ${groq.limiting_model.replace(/^openai\//, "")}`;
}

function AiFigures({ ai, latency, questions }) {
  const answered = questions.outcomes.answered || 0;
  const figures = [
    {
      label: "Tokens per question",
      value: ai.tokens_per_question,
      format: (v) => compact(v),
      caption:
        ai.calls_per_question != null
          ? `over ${ai.calls_per_question} model calls`
          : null,
    },
    {
      label: "Cost per answer",
      value: ai.cost_per_question ?? (questions.completed ? 0 : null),
      format: money,
      caption: ai.models.some((m) => m.kind === "llm" && !m.free_tier)
        ? `${money(ai.cost_usd)} in this period`
        : "Free tiers, not billed",
    },
    {
      label: "Confidence",
      value: ai.confidence,
      format: (v) => percent(v),
      caption: "average for answers given",
    },
    {
      label: "Sources cited",
      value: ai.sources,
      format: (v) => v.toFixed(1),
      caption: "per answer",
    },
    {
      label: "Second search",
      value: ai.search_retry_share,
      format: (v) => percent(v),
      caption: "of answers needed a refined search",
    },
    {
      label: "Answers revised",
      value: ai.revision_share,
      format: (v) => percent(v),
      caption: "rewritten after the fact check",
    },
  ];
  return (
    <section className="panel health-card" aria-labelledby="health-ai">
      <div className="health-card-heading">
        <h2 id="health-ai">AI quality and usage</h2>
        <p>
          {answered
            ? `Across ${whole.format(answered)} answered question${answered === 1 ? "" : "s"}`
            : "Figures appear once questions are answered"}
          {latency.p50_ms != null
            ? ` · typical answer ${duration(latency.p50_ms)}`
            : ""}
        </p>
      </div>
      <dl className="health-figures">
        {figures.map((f) => (
          <div key={f.label} className="health-figure-cell">
            <dt>{f.label}</dt>
            <dd>
              {f.value != null ? (
                <span className="health-number">
                  <AnimatedNumber value={f.value} format={f.format} />
                </span>
              ) : (
                <span className="health-number muted">Not yet</span>
              )}
              {f.caption && <span>{f.caption}</span>}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function StepBars({ steps }) {
  const max = Math.max(...steps.map((s) => s.avg_ms), 1);
  return (
    <section className="panel health-card" aria-labelledby="health-steps">
      <div className="health-card-heading">
        <h2 id="health-steps">Where the time goes</h2>
        <p>Average per answered question, by pipeline step</p>
      </div>
      {steps.length ? (
        <ul className="health-bars">
          {steps.map((step) => (
            <li key={step.label}>
              <span className="health-bar-label">{step.label}</span>
              <span className="health-bar-track">
                <span
                  className="health-bar"
                  style={{ width: `${(step.avg_ms / max) * 100}%` }}
                />
              </span>
              <span className="health-bar-value">
                {duration(step.avg_ms)}
                <small>{percent(step.share)}</small>
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="health-empty">
          Step times appear after the first answer.
        </p>
      )}
    </section>
  );
}

function Outcomes({ questions }) {
  const groups = OUTCOMES.map(([key, label, members]) => ({
    key,
    label,
    count: members.reduce((sum, m) => sum + (questions.outcomes[m] || 0), 0),
  })).filter((g) => g.count > 0);
  const total = groups.reduce((sum, g) => sum + g.count, 0);
  const limited = questions.outcomes.rate_limited || 0;
  return (
    <section className="panel health-card" aria-labelledby="health-outcomes">
      <div className="health-card-heading">
        <h2 id="health-outcomes">How questions ended</h2>
        <p>
          {total
            ? `${whole.format(total)} question${total === 1 ? "" : "s"} in this period`
            : "No questions in this period"}
        </p>
      </div>
      {total > 0 ? (
        <>
          <div
            className="health-segments"
            role="img"
            aria-label={groups
              .map(
                (g) => `${g.label}: ${g.count} (${percent(g.count / total)})`,
              )
              .join(", ")}
          >
            {groups.map((g) => (
              <span
                key={g.key}
                className={`health-segment outcome-${g.key}`}
                style={{ flexGrow: g.count }}
                title={`${g.label}: ${g.count} (${percent(g.count / total)})`}
              />
            ))}
          </div>
          <ul className="health-legend">
            {groups.map((g) => (
              <li key={g.key}>
                <span
                  className={`health-swatch outcome-${g.key}`}
                  aria-hidden="true"
                />
                <span className="health-legend-label">
                  {g.label}
                  {g.key === "errors" && limited > 0 && (
                    <small>{limited} rate-limited</small>
                  )}
                </span>
                <span className="health-legend-value">
                  {whole.format(g.count)}
                  <small>{percent(g.count / total)}</small>
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="health-empty">
          Outcomes appear after the first question.
        </p>
      )}
    </section>
  );
}

const KIND = {
  llm: "Language model",
  embedding: "Search embeddings",
  rerank: "Reranking",
};

function Models({ ai, groq }) {
  const groqModels = Object.fromEntries(
    (groq?.models || []).map((m) => [m.model, m]),
  );
  return (
    <section className="panel health-card" aria-labelledby="health-models">
      <div className="health-card-heading">
        <h2 id="health-models">Models</h2>
        <p>Calls and tokens in this period</p>
      </div>
      {ai.models.length ? (
        <div
          className="table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Model usage table"
        >
          <table className="health-table">
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col" className="num">
                  Calls
                </th>
                <th scope="col" className="num">
                  Tokens in
                </th>
                <th scope="col" className="num wide-only">
                  Tokens out
                </th>
                <th scope="col" className="num wide-only">
                  Cost
                </th>
              </tr>
            </thead>
            <tbody>
              {ai.models.map((m) => (
                <tr key={m.model}>
                  <th scope="row">
                    <span className="health-model">{m.model}</span>
                    <small>
                      {m.provider} · {KIND[m.kind] || m.kind}
                      {groqModels[m.model]?.tokens_per_question
                        ? ` · ${compact(groqModels[m.model].tokens_per_question)} tokens per question`
                        : ""}
                      {m.errors ? ` · ${m.errors} failed` : ""}
                      <span className="narrow-only">
                        {" · "}
                        {m.free_tier ? "free" : money(m.cost_usd || 0)}
                      </span>
                    </small>
                  </th>
                  <td className="num">{whole.format(m.calls)}</td>
                  <td className="num">{compact(m.input_tokens)}</td>
                  <td className="num wide-only">
                    {m.kind === "llm" ? compact(m.output_tokens) : "None"}
                  </td>
                  <td className="num wide-only">
                    {m.free_tier ? "Free" : money(m.cost_usd || 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="health-empty">
          Model usage appears after the first question.
        </p>
      )}
    </section>
  );
}

function Problems({ problems }) {
  // Keep the times in one column when only some problems link to a trace.
  const anyTrace = problems.recent.some((p) => p.trace_url);
  return (
    <section className="panel health-card" aria-labelledby="health-problems">
      <div className="health-card-heading">
        <h2 id="health-problems">Recent problems</h2>
        <p>
          {problems.total
            ? `${problems.total} in this period${problems.total > problems.recent.length ? `, latest ${problems.recent.length} shown` : ""}`
            : "Failed questions and searches show here"}
        </p>
      </div>
      {problems.recent.length ? (
        <ul className="activity-list health-problems">
          {problems.recent.map((p, i) => (
            <li key={`${p.at}-${i}`} className="activity-item">
              <span
                className={`activity-icon ${p.title.startsWith("Rate-limited") ? "warning" : "danger"}`}
              >
                <Icon
                  name={p.kind === "mcp" ? "terminal" : "alert"}
                  size={16}
                />
              </span>
              <div className="activity-text">
                <strong>
                  {p.title}
                  {p.username && (
                    <span className="activity-account">{p.username}</span>
                  )}
                </strong>
                <span>
                  {[p.detail, p.request_id && `Ref ${p.request_id.slice(0, 8)}`]
                    .filter(Boolean)
                    .join(" · ")}
                  <span className="narrow-only">
                    {p.detail || p.request_id ? " · " : ""}
                    {timeAgo(p.at)}
                  </span>
                </span>
              </div>
              <time
                className="activity-time"
                dateTime={p.at}
                title={formatDateTime(p.at)}
              >
                {timeAgo(p.at)}
              </time>
              {p.trace_url ? (
                <a
                  className="icon-button"
                  href={p.trace_url}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`Open the trace for ${p.title} in LangSmith`}
                  title="Open in LangSmith"
                >
                  <Icon name="external" size={16} />
                </a>
              ) : (
                anyTrace && <span className="health-trace-slot" />
              )}
            </li>
          ))}
        </ul>
      ) : (
        <div className="health-calm">
          <Icon name="done" size={18} />
          No problems in this period
        </div>
      )}
    </section>
  );
}

export default function AdminHealth() {
  const [range, setRange] = useState("24h");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const loaded = useRef(0);

  const latest = useRef(0);

  async function load(which = range) {
    // Only the newest request may update the page (a slow older one could still be in flight).
    const request = ++latest.current;
    setBusy(true);
    try {
      const next = await api.health(which, timeZone());
      if (request !== latest.current) return;
      setData(next);
      setError("");
      loaded.current = Date.now();
    } catch (e) {
      if (request === latest.current) setError(errorMessage(e));
    } finally {
      if (request === latest.current) setBusy(false);
    }
  }

  useEffect(() => {
    load(range);
    // Refresh while the tab is visible, and on returning to a stale tab.
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") load(range);
    }, REFRESH_MS);
    const visible = () => {
      if (
        document.visibilityState === "visible" &&
        Date.now() - loaded.current > REFRESH_MS
      )
        load(range);
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [range]);

  return (
    <div className="page health-page">
      <PageHeader
        title="Health"
        description="Whether everything is up, how the assistant is used, and how its AI is doing."
        actions={
          <>
            <div className="segmented" role="group" aria-label="Time range">
              {RANGES.map(([value, label]) => (
                <button
                  key={value}
                  aria-pressed={range === value}
                  onClick={() => setRange(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <button
              className="icon-button"
              onClick={() => load(range)}
              disabled={busy}
              aria-label="Refresh health"
              title="Refresh"
            >
              {busy ? <Spinner size={16} /> : <Icon name="refresh" size={16} />}
            </button>
          </>
        }
      />
      <Alert>{error}</Alert>
      {data === null ? (
        !error && (
          <section className="panel">
            <div className="panel-loading">
              <Spinner /> Checking services…
            </div>
          </section>
        )
      ) : (
        <div className="health-body" aria-busy={busy || undefined}>
          <StatusPanel data={data} />
          <Tiles data={data} range={data.range} />
          <AiFigures
            ai={data.ai}
            latency={data.latency}
            questions={data.questions}
          />
          <div className="health-pair">
            <StepBars steps={data.latency.steps} />
            <Outcomes questions={data.questions} />
          </div>
          <Models ai={data.ai} groq={data.groq} />
          <Problems problems={data.problems} />
        </div>
      )}
    </div>
  );
}
