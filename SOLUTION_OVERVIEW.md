# Multi-Agent Incident Analysis Suite — Solution Overview

## 1. Problem Statement

When a production incident happens, an on-call engineer typically has to:

1. Manually scroll through hundreds or thousands of lines of raw logs to figure out *what* went wrong.
2. Apply tribal/institutional knowledge to work out *why* it went wrong and *how* to fix it.
3. Write up a human-readable summary and post it to the team (Slack).
4. File a tracking ticket (JIRA) with enough context for follow-up work.
5. Do all of this under time pressure, often duplicating tickets for recurring issues because there's no systematic de-duplication.

This is slow, inconsistent across engineers, hard to audit (recommendations aren't traceable back to the exact log evidence that produced them), and doesn't scale — every incident starts from a blank page.

**The need:** an automated pipeline that ingests a raw log file, identifies distinct operational issues, proposes concrete remediation, produces incident documentation, and pushes it to the team's existing tools (Slack, JIRA) — while keeping a human in the loop before anything external happens, and keeping every claim traceable back to the source log lines.

## 2. Solution Implemented

A multi-agent pipeline (FastAPI + LangGraph) that turns a log upload into a fully documented, triaged incident with one human approval step:

| Capability | Implementation |
|---|---|
| **Orchestration** | LangGraph `StateGraph` — 8 nodes, each with retry, timeout, and checkpointing so a killed process resumes exactly where it left off |
| **LLM layer** | Provider-agnostic — any OpenAI-compatible Chat Completions endpoint. Swappable via 3 env vars (`IAS_LLM_BASE_URL` / `IAS_LLM_API_KEY` / `IAS_MODEL`). Currently wired to Gemini (`gemini-2.5-flash`, free tier), also verified against OpenRouter. Falls back to a deterministic rule-based mock when no key is configured, so CI and offline demos never need live credentials |
| **Structured output** | Every LLM call is validated against a Pydantic schema; on a validation failure, one auto-repair retry is sent back to the model with the exact error before giving up |
| **Log preprocessing** | Secret redaction, line-based chunking with overlap (to keep issues that straddle a chunk boundary intact), and fingerprint-based de-duplication so the same recurring error isn't reported N times |
| **Notifications** | Slack (Block Kit message with a clickable deep link straight to the run in the UI) and JIRA (ticket per Critical/High issue, with a real clickable link back to the run, and fingerprint-labeled duplicate suppression) |
| **Human-in-the-loop** | A `gate` node blocks Slack/JIRA until a human approves via the UI (or auto-approves if explicitly configured) |
| **Auth** | Bearer API key for programmatic/API clients; the browser UI instead gets a signed, `HttpOnly`, expiring session cookie issued on page load — the raw API key never reaches client-side JS |
| **Resilience** | Per-node retry with exponential backoff, hard timeouts, graceful degradation (a failed node doesn't crash the run, it just marks that node `failed` and the run finishes as `partial`), and a hard token-budget cap per run |
| **Observability (basic)** | SSE stream of live node status to the UI, an audit log per run, and a token/cost meter per run |
| **Corporate-network compatibility** | All outbound HTTPS calls (LLM, Slack, JIRA) trust the OS certificate store via `truststore`, so the app works behind TLS-inspecting corporate proxies without disabling certificate verification |

## 3. Flow

### End-to-end pipeline

```
Upload log
   │
   ▼
┌─────────┐   redact secrets, chunk lines (200 lines/chunk, 20 overlap)
│ ingest  │
└────┬────┘
     ▼
┌─────────┐   parallel LLM call per chunk → classify issues → merge/dedupe by
│classify │   fingerprint, keep the highest severity seen for each fingerprint
└────┬────┘
     ▼
┌─────────┐   one LLM call per unique issue → root cause, numbered fix steps,
│remediate│   rationale, risk level
└────┬────┘
     ▼
┌─────────┐   one LLM call → Markdown incident-response checklist
│cookbook │   (Immediate Actions / Verification / Prevention / Escalate If)
└────┬────┘
     ▼
┌─────────┐   pauses the run and waits (up to 10 min) for a human to
│  gate   │   click Approve/Reject in the UI — unless auto-post is enabled
└────┬────┘
     ▼ (if approved)
┌─────────┐   posts a Slack message with top issues + a link back to this run
│  slack  │
└────┬────┘
     │
     ├─── any issue Critical/High? ──► ┌──────┐  files one JIRA ticket per
     │                                  │ jira │  Critical/High issue, skips
     │                                  └──┬───┘  ones already open (by fp label)
     ▼                                     ▼
┌──────────┐◄──────────────────────────────┘
│ finalize │
└──────────┘
```

Every node writes a checkpoint to the database before and after running. If the process dies mid-run (crash, deploy, `docker kill`), restarting the app picks the run back up from its last checkpoint instead of starting over.

### What the human actually sees

1. Open the app → browser gets a signed session cookie automatically (no key handling in the UI).
2. Drop a log file → pipeline starts, node-by-node progress streams in live over SSE.
3. Once `cookbook` completes, the run pauses at `gate` — the UI shows an approve/reject banner.
4. On approval: Slack fires immediately; JIRA fires only for Critical/High issues, each with a link back to this exact run and any prior open ticket for the same fingerprint reused instead of duplicated.
5. Full run history is browsable, and every issue can be expanded to show the exact log lines (with 3 lines of surrounding context) that produced it.

## 4. Future Scope

- **Multi-provider failover** — if the primary LLM endpoint 404s/rate-limits (as happened with an org-restricted OpenRouter key), automatically fall back to a secondary configured provider instead of failing the run.
- **Real multi-user auth** — replace the single shared API key with per-user accounts/roles (the current session-cookie design was scoped to fix "secret in client-side JS," not to add multi-tenancy).
- **Rate-limit-aware queuing** — Gemini's free tier caps out at ~10–15 requests/minute; a queue/backoff layer would let larger logs process reliably instead of racing the limit.
- **Semantic de-duplication** — current dedup is exact-fingerprint-hash based; a large recurring issue with slightly different wording currently creates a second entry. Embedding-based similarity would catch near-duplicates.
- **Cross-run trend analysis** — surface "this fingerprint has recurred 5 times this month" instead of treating every run in isolation.
- **More integrations** — PagerDuty, MS Teams, ServiceNow, email digests, following the same dry-run-when-unconfigured pattern already used for Slack/JIRA.
- **Configurable routing rules** — severity → destination mapping is currently hardcoded (JIRA only fires for Critical/High); make this configurable per team.
- **Observability upgrade** — structured logs, OpenTelemetry tracing across the LangGraph nodes, and a dashboard for token spend / failure rate / pipeline latency instead of the current basic `/metrics` counters.
- **Prompt regression testing** — a golden-set eval harness so changes to the classify/remediate/cookbook prompts can be checked against known-good outputs before shipping.
- **Live log ingestion** — beyond static file upload, tail logs directly from CloudWatch/Datadog/etc. and trigger runs automatically on error-rate spikes.
- **Guarded auto-remediation** — today the pipeline only *proposes* fix steps; a tightly-scoped, opt-in execution mode (e.g. pre-approved, non-destructive runbook actions) could close the loop further.
