# Multi-Agent DevOps Incident Analysis Suite

Upload ops logs → AI agents triage, remediate, document, notify Slack, and file JIRA tickets.  
Every recommendation is linked back to the exact log lines that caused it.

## Quick start (2 commands)

```bash
cp .env.example .env      # fill in your API keys
docker compose up         # api on :8000, UI on http://localhost:8000
```

## Without Docker

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
# open http://localhost:8000
```

## Run tests (no API key needed — uses deterministic mock)

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

## Module guide

| File | What it does |
|---|---|
| `app/config.py` | All settings from env vars via Pydantic Settings |
| `app/schemas.py` | Typed contracts for every boundary (API / LLM / integrations) |
| `app/db.py` | SQLAlchemy async ORM, run checkpoints, audit log |
| `app/pipeline/preprocess.py` | Secret redaction, chunking, fingerprint deduplication |
| `app/pipeline/llm.py` | OpenRouter client + deterministic mock, token meter, repair retry |
| `app/integrations/clients.py` | Slack + JIRA adapters, idempotent, dry-run when unconfigured |
| `app/pipeline/graph.py` | LangGraph StateGraph — 8-node pipeline with parallel fan-out |
| `app/main.py` | FastAPI: upload, SSE, approval, resume-on-startup, metrics |
| `tests/test_pipeline.py` | Unit + E2E tests against golden demo log bundle |

## Environment variables (all prefixed `IAS_`)

| Variable | Default | Description |
|---|---|---|
| `IAS_API_KEY` | `dev-key-change-me` | Bearer token for the REST API |
| `IAS_DATABASE_URL` | SQLite | Switch to `postgresql+asyncpg://...` for prod |
| `IAS_PUBLIC_BASE_URL` | `http://localhost:8000` | Used to build the run link in Slack/JIRA notifications |
| `IAS_LLM_API_KEY` | *(empty)* | Leave empty → deterministic mock mode |
| `IAS_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint (OpenRouter, Gemini, OpenAI, ...) |
| `IAS_MODEL` | `openai/gpt-4o-mini` | Model id, in the format the chosen endpoint expects |
| `IAS_SLACK_BOT_TOKEN` | *(empty)* | Leave empty → dry-run Slack |
| `IAS_SLACK_CHANNEL` | *(empty)* | Channel ID or name |
| `IAS_SLACK_AUTO_POST` | `false` | Skip approval gate for Slack |
| `IAS_JIRA_BASE_URL` | *(empty)* | e.g. `https://yourorg.atlassian.net` |
| `IAS_JIRA_EMAIL` | *(empty)* | Atlassian account email |
| `IAS_JIRA_API_TOKEN` | *(empty)* | Atlassian API token |
| `IAS_JIRA_PROJECT_KEY` | `OPS` | Target JIRA project |
| `IAS_JIRA_ISSUE_TYPE` | `Bug` | Must exist in the project's issue-type scheme |
| `IAS_JIRA_AUTO_CREATE` | `false` | Skip approval gate for JIRA |

## Demo: checkpoint resume (the judge moment)

1. Upload `demo/broken_prod.log`
2. Watch the pipeline reach the gate node
3. `docker kill incident-suite-api-1`   ← kill the container mid-run
4. `docker compose up`                  ← restart
5. The run resumes exactly where it left off
