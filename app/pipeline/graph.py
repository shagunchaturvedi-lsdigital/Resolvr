"""LangGraph orchestration.

Graph:
  ingest → classify (parallel fan-out) → merge → remediate
        → cookbook → gate → [slack?] → [jira?] → finalize

Production behaviors:
- Checkpoint to Postgres/SQLite after every node (kill the process → run resumes).
- Per-node retry + timeout; failures degrade to partial results, never crash the run.
- Token budget hard cap.
- Approval gate for Slack/JIRA (human-in-the-loop) unless auto mode is enabled.
"""
from __future__ import annotations

import asyncio
import traceback
from typing import Any, Callable, TypedDict

from langgraph.graph import StateGraph, END

from ..config import get_settings
from ..db import save_checkpoint, audit
from ..schemas import (ClassifierOut, CookbookOut, EvidenceRef, Issue,
                       Remediation, RemediationOut, Severity)
from ..integrations.clients import post_slack, create_jira_ticket
from .llm import LLMClient, TokenMeter
from .preprocess import Chunk, chunk_lines, fingerprint, redact

SEV_ORDER = ["Critical", "High", "Medium", "Low"]


class PipelineState(TypedDict, total=False):
    run_id: str
    filename: str
    raw_text: str
    redactions: int
    chunks: list[dict]
    issues: list[dict]
    remediations: list[dict]
    cookbook_md: str
    slack: dict | None
    jira: list[dict]
    approved: bool | None          # None = waiting, True/False = decided
    errors: list[str]
    nodes: dict[str, str]
    tokens_in: int
    tokens_out: int
    cost_usd: float


EventCb = Callable[[str, str, str, str], Any]  # run_id, node, status, detail


class Pipeline:
    def __init__(self, emit: EventCb):
        self.emit = emit
        self.settings = get_settings()
        self.graph = self._build()

    # ---- node wrapper: retry, timeout, checkpoint, events, degradation ----
    def node(self, name: str, fn):
        async def wrapped(state: PipelineState) -> PipelineState:
            meter = TokenMeter()
            meter.tokens_in, meter.tokens_out = state.get("tokens_in", 0), state.get("tokens_out", 0)
            state["nodes"] = {**state.get("nodes", {}), name: "running"}
            await self.emit(state["run_id"], name, "running", "")
            await save_checkpoint(state["run_id"], dict(state), state["nodes"], "running")
            last: Exception | None = None
            for attempt in range(self.settings.node_retries):
                try:
                    result = await asyncio.wait_for(fn(state, meter), timeout=self.settings.node_timeout_s)
                    state.update(result)
                    state["tokens_in"], state["tokens_out"] = meter.tokens_in, meter.tokens_out
                    state["cost_usd"] = meter.cost_usd
                    if meter.tokens_in + meter.tokens_out > self.settings.token_budget_per_run:
                        raise RuntimeError("token budget exceeded")
                    state["nodes"][name] = "done"
                    await self.emit(state["run_id"], name, "done", "")
                    await save_checkpoint(state["run_id"], dict(state), state["nodes"], "running")
                    return state
                except Exception as e:  # noqa: BLE001
                    last = e
                    await asyncio.sleep(min(2 ** attempt, 8))
            # graceful degradation
            state.setdefault("errors", []).append(f"{name}: {last}")
            state["nodes"][name] = "failed"
            await self.emit(state["run_id"], name, "failed", str(last)[:200])
            await save_checkpoint(state["run_id"], dict(state), state["nodes"], "running")
            return state
        return wrapped

    # ---------------------------- nodes ----------------------------------
    async def ingest(self, state: PipelineState, meter: TokenMeter) -> dict:
        masked, n = redact(state["raw_text"])
        chunks = chunk_lines(state["filename"], masked,
                             self.settings.chunk_lines, self.settings.chunk_overlap)
        await audit(state["run_id"], "ingest", f"{len(chunks)} chunks, {n} redactions")
        return {"redactions": n,
                "chunks": [c.__dict__ for c in chunks],
                "raw_text": masked}

    async def classify(self, state: PipelineState, meter: TokenMeter) -> dict:
        llm = LLMClient(meter)
        sem = asyncio.Semaphore(5)

        async def one(cd: dict) -> ClassifierOut:
            async with sem:
                numbered = "\n".join(
                    f"{cd['start_line'] + i}: {ln}" for i, ln in enumerate(cd["text"].splitlines()))
                return await llm.structured(
                    system=("You are an SRE log classifier. Identify distinct operational issues in the "
                            "log chunk. Categories: Infrastructure, Application Error, Configuration, "
                            "Security, Performance, Dependency/Network, Data/DB, Unknown. Severities: "
                            "Critical, High, Medium, Low. Use the printed line numbers for start/end. "
                            'Schema: {"issues":[{"category","severity","confidence","summary",'
                            '"error_signature","start_line","end_line","timestamp_first",'
                            '"timestamp_last","occurrences"}]}'),
                    user=numbered, schema=ClassifierOut)

        results = await asyncio.gather(*(one(c) for c in state["chunks"]), return_exceptions=True)
        # merge + dedupe by fingerprint
        merged: dict[str, Issue] = {}
        for cd, res in zip(state["chunks"], results):
            if isinstance(res, Exception):
                state.setdefault("errors", []).append(f"classify chunk L{cd['start_line']}: {res}")
                continue
            for it in res.issues:
                fp = fingerprint(it.error_signature)
                ev = EvidenceRef(file=cd["file"], start_line=it.start_line, end_line=it.end_line)
                if fp in merged:
                    m = merged[fp]
                    m.count += it.occurrences
                    m.evidence.append(ev)
                    m.last_seen = it.timestamp_last or m.last_seen
                    if SEV_ORDER.index(it.severity) < SEV_ORDER.index(m.severity):
                        m.severity = it.severity
                else:
                    merged[fp] = Issue(fingerprint=fp, category=it.category, severity=it.severity,
                                       confidence=it.confidence, summary=it.summary, count=it.occurrences,
                                       first_seen=it.timestamp_first, last_seen=it.timestamp_last,
                                       evidence=[ev])
        issues = sorted(merged.values(), key=lambda i: (SEV_ORDER.index(i.severity), -i.count))
        await audit(state["run_id"], "classify", f"{len(issues)} unique issues")
        return {"issues": [i.model_dump() for i in issues]}

    async def remediate(self, state: PipelineState, meter: TokenMeter) -> dict:
        llm = LLMClient(meter)
        rems: list[dict] = []
        for i in state.get("issues", []):
            try:
                out: RemediationOut = await llm.structured(
                    system=("You are a senior SRE. Given one detected issue, produce root cause, "
                            "concrete numbered fix steps (shell commands where applicable), rationale, "
                            'risk (must be exactly one of: low, medium, high — lowercase, no other '
                            "words), destructive flag, citations. Citations must be short internal "
                            "references only (e.g. \"runbook:db-standard\"), never a URL or markdown "
                            'link. Schema: {"root_cause","fix_steps_md","rationale","risk","destructive",'
                            '"citations"}'),
                    user=(f"Issue: {i['summary']}\nCategory: {i['category']}\nSeverity: {i['severity']}\n"
                          f"Occurrences: {i['count']}\nEvidence refs: {i['evidence']}"),
                    schema=RemediationOut)
                rems.append(Remediation(fingerprint=i["fingerprint"], **out.model_dump()).model_dump())
            except Exception as e:  # noqa: BLE001
                state.setdefault("errors", []).append(f"remediate {i['fingerprint']}: {e}")
        await audit(state["run_id"], "remediate", f"{len(rems)} remediations")
        return {"remediations": rems}

    async def cookbook(self, state: PipelineState, meter: TokenMeter) -> dict:
        llm = LLMClient(meter)
        ctx = "\n".join(f"- [{i['severity']}] {i['summary']} (×{i['count']})" for i in state.get("issues", []))
        fixes = "\n".join(f"- {r['fingerprint']}: {r['root_cause']}" for r in state.get("remediations", []))
        out: CookbookOut = await llm.structured(
            system=("Synthesize an incident-response checklist in Markdown with sections: Immediate "
                    'Actions, Verification, Prevention, Escalate If. Schema: {"content_md"}'),
            user=f"Issues:\n{ctx}\n\nRoot causes:\n{fixes}", schema=CookbookOut, max_tokens=1500)
        return {"cookbook_md": out.content_md}

    async def gate(self, state: PipelineState, meter: TokenMeter) -> dict:
        """Human-in-the-loop approval before external actions (unless auto mode)."""
        if self.settings.slack_auto_post and self.settings.jira_auto_create:
            return {"approved": True}
        if state.get("approved") is None:
            await self.emit(state["run_id"], "gate", "waiting_approval",
                            "Awaiting approval for Slack/JIRA actions")
            await save_checkpoint(state["run_id"], dict(state), state.get("nodes", {}), "waiting_approval")
            # poll the checkpoint for a decision (approval endpoint updates it)
            from ..db import load_run
            import json as _json
            for _ in range(600):  # up to 10 min
                await asyncio.sleep(1)
                row = await load_run(state["run_id"])
                if row:
                    st = _json.loads(row.state_json)
                    if st.get("approved") is not None:
                        return {"approved": st["approved"]}
            return {"approved": False}
        return {"approved": state["approved"]}

    async def slack_node(self, state: PipelineState, meter: TokenMeter) -> dict:
        if not state.get("approved"):
            state["nodes"]["slack"] = "skipped"
            return {"slack": {"ok": False, "detail": "not approved", "skipped": True}}
        res = await post_slack(state["run_id"], state.get("issues", []), state.get("filename"))
        await audit(state["run_id"], "slack_post", str(res)[:500], actor="agent")
        return {"slack": res}

    async def jira_node(self, state: PipelineState, meter: TokenMeter) -> dict:
        if not state.get("approved"):
            state["nodes"]["jira"] = "skipped"
            return {"jira": []}
        rems = {r["fingerprint"]: r for r in state.get("remediations", [])}
        results = []
        for i in state.get("issues", []):
            if i["severity"] in ("Critical", "High"):
                res = await create_jira_ticket(state["run_id"], i, rems.get(i["fingerprint"]))
                results.append(res)
                await audit(state["run_id"], "jira_create", str(res)[:500], actor="agent")
        return {"jira": results}

    async def finalize(self, state: PipelineState, meter: TokenMeter) -> dict:
        return {}

    # ---------------------------- graph ----------------------------------
    def _build(self):
        g = StateGraph(PipelineState)
        g.add_node("ingest", self.node("ingest", self.ingest))
        g.add_node("classify", self.node("classify", self.classify))
        g.add_node("remediate", self.node("remediate", self.remediate))
        g.add_node("cookbook", self.node("cookbook", self.cookbook))
        g.add_node("gate", self.node("gate", self.gate))
        g.add_node("slack", self.node("slack", self.slack_node))
        g.add_node("jira", self.node("jira", self.jira_node))
        g.add_node("finalize", self.node("finalize", self.finalize))

        g.set_entry_point("ingest")
        g.add_edge("ingest", "classify")
        g.add_edge("classify", "remediate")
        g.add_edge("remediate", "cookbook")
        g.add_edge("cookbook", "gate")
        g.add_edge("gate", "slack")
        # conditional: JIRA only when Critical/High issues exist
        g.add_conditional_edges(
            "slack",
            lambda s: "jira" if any(i["severity"] in ("Critical", "High") for i in s.get("issues", [])) else "finalize",
            {"jira": "jira", "finalize": "finalize"},
        )
        g.add_edge("jira", "finalize")
        g.add_edge("finalize", END)
        return g.compile()

    async def run(self, state: PipelineState) -> PipelineState:
        try:
            final: PipelineState = await self.graph.ainvoke(state)
            failed = [n for n, s in final.get("nodes", {}).items() if s == "failed"]
            status = "partial" if failed else "complete"
            if any("token budget" in e for e in final.get("errors", [])):
                status = "budget_exceeded"
            await save_checkpoint(final["run_id"], dict(final), final.get("nodes", {}), status)
            await self.emit(final["run_id"], "pipeline", status, "")
            return final
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            state.setdefault("errors", []).append(f"pipeline: {e}")
            await save_checkpoint(state["run_id"], dict(state), state.get("nodes", {}), "failed")
            await self.emit(state["run_id"], "pipeline", "failed", str(e)[:200])
            return state
