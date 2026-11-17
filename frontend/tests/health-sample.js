// Sample responses for GET /v1/admin/health, shaped like the real endpoint's.
// state: "busy" (normal use), "limited" (Groq rate limit), "down" (search down), "empty".
export function sample(range = "24h", state = "busy") {
  const n = range === "24h" ? 24 : range === "7d" ? 7 : 30;
  const now = new Date();
  const series = Array.from({ length: n }, (_, i) => {
    const d = new Date(now);
    if (range === "24h") d.setHours(now.getHours() - (n - 1 - i), 0, 0, 0);
    else d.setDate(now.getDate() - (n - 1 - i));
    const pad = (x) => String(x).padStart(2, "0");
    const t = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(range === "24h" ? d.getHours() : 0)}:00:00`;
    const q =
      state === "empty" ? 0 : [0, 0, 1, 0, 2, 3, 1, 0, 4, 2, 5, 3][i % 12];
    return {
      t,
      questions: q,
      problems: q > 3 ? 1 : 0,
      p50_ms: q ? 14000 + ((i * 3779) % 9000) : null,
      mcp_calls: state === "empty" ? 0 : [3, 0, 8, 12, 0, 5][i % 6],
    };
  });
  const empty = state === "empty";
  const limited = state === "limited";
  return {
    generated_at: now.toISOString(),
    range,
    overall: limited
      ? {
          state: "warn",
          message: "Answers: Rate-limited · try again in about 6 min",
        }
      : state === "down"
        ? { state: "down", message: "Search: Search database not responding" }
        : { state: "ok", message: "All systems working" },
    checks: [
      { key: "website", label: "Website", state: "ok", detail: "Up 2 days" },
      limited
        ? {
            key: "answers",
            label: "Answers",
            state: "warn",
            detail: "Rate-limited · try again in about 6 min",
          }
        : {
            key: "answers",
            label: "Answers",
            state: "ok",
            detail: "Answering normally",
          },
      state === "down"
        ? {
            key: "search",
            label: "Search",
            state: "down",
            detail: "Search database not responding",
          }
        : {
            key: "search",
            label: "Search",
            state: "ok",
            detail: "41,233 passages",
          },
      {
        key: "reranker",
        label: "Reranker",
        state: "ok",
        detail: "Voyage rerank-3 · used 12 min ago",
      },
      {
        key: "backups",
        label: "Backups",
        state: "ok",
        detail: "Last 5 h ago · 2.4 MB",
      },
    ],
    release: {
      version: "c55bc85",
      environment: "production",
      started_at: now.toISOString(),
      database_bytes: 24_000_000,
      queue_size: 1,
    },
    series,
    questions: empty
      ? { total: 0, previous: 0, completed: 0, people: 0, outcomes: {} }
      : {
          total: 31,
          previous: 26,
          completed: 27,
          people: 4,
          outcomes: {
            answered: 22,
            awaiting_approval: 3,
            no_evidence: 2,
            blocked: 1,
            rate_limited: 2,
            failed: 1,
          },
        },
    latency: empty
      ? { p50_ms: null, p95_ms: null, queued: 0, queue_avg_ms: null, steps: [] }
      : {
          p50_ms: 18400,
          p95_ms: 41200,
          queued: limited ? 3 : 0,
          queue_avg_ms: 5200,
          steps: [
            { label: "Checking relevance", avg_ms: 8400, share: 0.44 },
            { label: "Writing the answer", avg_ms: 4100, share: 0.21 },
            { label: "Searching filings", avg_ms: 3100, share: 0.16 },
            { label: "Checking the answer", avg_ms: 1600, share: 0.08 },
            { label: "Safety checks", avg_ms: 1100, share: 0.06 },
            { label: "Ranking passages", avg_ms: 600, share: 0.03 },
            { label: "Understanding the question", avg_ms: 400, share: 0.02 },
          ],
        },
    ai: empty
      ? {
          tokens_per_question: null,
          calls_per_question: null,
          input_tokens: 0,
          output_tokens: 0,
          cost_usd: 0,
          cost_per_question: null,
          confidence: null,
          sources: null,
          search_retry_share: null,
          revision_share: null,
          models: [],
        }
      : {
          tokens_per_question: 14200,
          calls_per_question: 14,
          input_tokens: 388000,
          output_tokens: 41000,
          cost_usd: 0,
          cost_per_question: 0,
          confidence: 0.91,
          sources: 3.4,
          search_retry_share: 0.12,
          revision_share: 0.08,
          models: [
            {
              model: "openai/gpt-oss-20b",
              kind: "llm",
              provider: "Groq",
              calls: 312,
              input_tokens: 281000,
              output_tokens: 29000,
              errors: 2,
              questions: 27,
              cost_usd: 0,
              free_tier: true,
            },
            {
              model: "openai/gpt-oss-120b",
              kind: "llm",
              provider: "Groq",
              calls: 62,
              input_tokens: 107000,
              output_tokens: 12000,
              errors: 0,
              questions: 27,
              cost_usd: 0,
              free_tier: true,
            },
            {
              model: "rerank-3",
              kind: "rerank",
              provider: "Voyage",
              calls: 29,
              input_tokens: 612000,
              output_tokens: 0,
              errors: 0,
              questions: 27,
              cost_usd: 0,
              free_tier: true,
            },
            {
              model: "voyage-finance-2",
              kind: "embedding",
              provider: "Voyage",
              calls: 35,
              input_tokens: 520,
              output_tokens: 0,
              errors: 0,
              questions: 27,
              cost_usd: 0,
              free_tier: true,
            },
          ],
        },
    groq: {
      models: [
        {
          model: "openai/gpt-oss-120b",
          daily_limit: 200000,
          used_24h: 58000,
          remaining: limited ? 9000 : 142000,
          share_left: limited ? 0.045 : 0.71,
          tokens_per_question: 14500,
          questions_left: limited ? 0 : 9,
          requests_remaining: 941,
          requests_limit: 1000,
          limited_now: limited,
          retry_after_seconds: limited ? 360 : null,
        },
        {
          model: "openai/gpt-oss-20b",
          daily_limit: 200000,
          used_24h: 70000,
          remaining: 130000,
          share_left: 0.65,
          tokens_per_question: 11700,
          questions_left: 11,
          requests_remaining: 702,
          requests_limit: 1000,
          limited_now: false,
          retry_after_seconds: null,
        },
      ],
      questions_left: limited ? 0 : 9,
      share_left: limited ? 0.045 : 0.71,
      limiting_model: "openai/gpt-oss-120b",
      estimated: true,
    },
    mcp: empty
      ? {
          calls: 0,
          people: 0,
          searches: 0,
          page_reads: 0,
          listings: 0,
          failed: 0,
          search_p50_ms: null,
        }
      : {
          calls: 128,
          people: 3,
          searches: 96,
          page_reads: 27,
          listings: 5,
          failed: 1,
          search_p50_ms: 820,
        },
    problems: empty
      ? { total: 0, recent: [] }
      : {
          total: 4,
          recent: [
            {
              kind: "question",
              at: new Date(now - 7 * 60000).toISOString(),
              title: "Rate-limited during relevance check",
              detail: "Groq · openai/gpt-oss-20b · retry after 360 s",
              username: "hr",
              request_id: "7f3a2c91-1111-2222-3333-444455556666",
              trace_url:
                "https://smith.langchain.com/o/x/projects/p/y/r/7f3a2c91?poll=true",
            },
            {
              kind: "mcp",
              at: new Date(now - 95 * 60000).toISOString(),
              title: "MCP search documents failed",
              detail: "ConnectTimeout",
              username: "analyst",
            },
            {
              kind: "question",
              at: new Date(now - 5 * 3600000).toISOString(),
              title: "Failed during answer writing",
              detail: null,
              username: "finance",
              request_id: "1b9e04aa-1111-2222-3333-444455556666",
              trace_url: null,
            },
          ],
        },
    langsmith: {
      enabled: true,
      project: "rag-agent-prod",
      project_url: "https://smith.langchain.com/o/x/projects/p/y",
    },
  };
}
