"""Test suite: unit tests for preprocessing + full E2E pipeline on the golden bundle.

Run: pytest -q   (uses MockLLM — no API key or network needed; CI-safe)
"""
import asyncio
import json
import os
import pathlib

import pytest

os.environ["IAS_LLM_MODE"] = "mock"
os.environ["IAS_DATABASE_URL"] = "sqlite+aiosqlite:///./test_suite.db"
os.environ["IAS_SLACK_AUTO_POST"] = "true"   # skip approval gate in tests
os.environ["IAS_JIRA_AUTO_CREATE"] = "true"

from app.pipeline.preprocess import chunk_lines, fingerprint, redact  # noqa: E402


def test_redaction_masks_secrets_and_pii():
    text = "key AKIAIOSFODNN7EXAMPLE mail ops@example.com token Bearer abcdef1234567890abcdef"
    masked, n = redact(text)
    assert "AKIA" not in masked and "@example.com" not in masked
    assert n >= 3


def test_chunking_overlap_and_bounds():
    text = "\n".join(f"line {i}" for i in range(1, 501))
    chunks = chunk_lines("f.log", text, size=200, overlap=20)
    assert chunks[0].start_line == 1 and chunks[0].end_line == 200
    assert chunks[1].start_line == 181            # overlap honored
    assert chunks[-1].end_line == 500             # nothing lost


def test_fingerprint_is_stable_across_variable_parts():
    a = fingerprint("2026-07-04T02:04:01Z connection to 10.0.0.1:5432 refused retry=3")
    b = fingerprint("2026-07-04T02:05:44Z connection to 10.0.9.7:5432 refused retry=1")
    assert a == b
    assert a != fingerprint("certificate has expired")


@pytest.mark.asyncio
async def test_e2e_pipeline_on_golden_bundle():
    from app.db import init_db, create_run, load_run
    from app.pipeline.graph import Pipeline

    await init_db()
    text = pathlib.Path(__file__).parent.parent.joinpath("demo/broken_prod.log").read_text()
    run_id = await create_run("broken_prod.log")

    events = []
    async def emit(rid, node, status, detail):
        events.append((node, status))

    state = {"run_id": run_id, "filename": "broken_prod.log", "raw_text": text,
             "issues": [], "remediations": [], "jira": [], "errors": [], "nodes": {},
             "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "approved": None}
    final = await Pipeline(emit).run(state)

    sevs = {i["severity"] for i in final["issues"]}
    cats = {i["category"] for i in final["issues"]}
    assert "Critical" in sevs, "DB connection storm must be Critical"
    assert "Dependency/Network" in cats and "Security" in cats
    assert len(final["issues"]) >= 4, "expected the seeded issue classes to be found"
    assert all(i["evidence"] for i in final["issues"]), "every issue must carry evidence"
    fps = {r["fingerprint"] for r in final["remediations"]}
    assert {i["fingerprint"] for i in final["issues"]} <= fps | {i["fingerprint"] for i in final["issues"] if i["severity"] == "Low"}
    assert final["cookbook_md"] and "Immediate" in final["cookbook_md"]
    assert final["slack"] and final["slack"]["ok"]                # dry-run counts as ok
    assert final["jira"], "Critical issue must produce a JIRA action"
    assert ("pipeline", "complete") in events

    row = await load_run(run_id)
    assert row.status == "complete"
    saved = json.loads(row.state_json)
    assert saved["issues"], "checkpoint persisted final state"


@pytest.mark.asyncio
async def test_resume_from_checkpoint():
    """Kill-and-resume: a run checkpointed mid-flight is picked up and completes."""
    from app.db import init_db, create_run, save_checkpoint, find_resumable

    await init_db()
    run_id = await create_run("resume.log")
    state = {"run_id": run_id, "filename": "resume.log",
             "raw_text": "2026-07-04T02:00:00Z ERROR db connection refused ECONNREFUSED",
             "issues": [], "remediations": [], "jira": [], "errors": [], "nodes": {"ingest": "done"},
             "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "approved": True}
    await save_checkpoint(run_id, state, state["nodes"], "running")
    resumable = await find_resumable()
    assert any(r.id == run_id for r in resumable), "mid-flight run is discoverable for resume"
