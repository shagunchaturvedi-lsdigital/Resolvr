"""FastAPI application: REST API + SSE + static UI + resume-on-startup."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings
from .db import (audit, create_run, delete_run, find_resumable, init_db,
                 list_runs, load_run, save_checkpoint)
from .pipeline.graph import Pipeline, PipelineState
from .schemas import ApproveAction, RunStatus

security = HTTPBearer(auto_error=False)

# ---- browser session cookie ----
# The API key itself is a server-side secret and is never sent to the browser.
# GET "/" issues a signed, expiring session cookie instead; the frontend relies
# on the browser sending it automatically. The Bearer header path stays available
# for API clients (curl, automation) that already hold the raw key.
SESSION_COOKIE = "ias_session"
SESSION_TTL_S = 24 * 3600


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_session_token(secret: str) -> str:
    expiry = str(int(time.time()) + SESSION_TTL_S)
    return f"{expiry}.{_sign(expiry, secret)}"


def _valid_session_token(token: str | None, secret: str) -> bool:
    if not token or "." not in token:
        return False
    expiry, sig = token.split(".", 1)
    if not (expiry.isdigit() and hmac.compare_digest(_sign(expiry, secret), sig)):
        return False
    return int(expiry) > time.time()


def require_key(request: Request, creds: HTTPAuthorizationCredentials = Depends(security)) -> None:
    settings = get_settings()
    if creds and hmac.compare_digest(creds.credentials, settings.api_key):
        return
    if _valid_session_token(request.cookies.get(SESSION_COOKIE), settings.api_key):
        return
    raise HTTPException(401, "invalid or missing API key")


# ---- in-process event bus for SSE ----
class EventBus:
    def __init__(self) -> None:
        self.subscribers: dict[str, list[asyncio.Queue]] = {}

    async def emit(self, run_id: str, node: str, status: str, detail: str) -> None:
        evt = {"run_id": run_id, "node": node, "status": status, "detail": detail}
        for q in self.subscribers.get(run_id, []):
            await q.put(evt)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        self.subscribers.get(run_id, []).remove(q)


bus = EventBus()
metrics = {"runs_total": 0, "runs_failed": 0, "node_events": 0}


async def launch(state: PipelineState) -> None:
    metrics["runs_total"] += 1
    pipe = Pipeline(emit=_emit)
    final = await pipe.run(state)
    if final.get("nodes", {}).get("pipeline") == "failed":
        metrics["runs_failed"] += 1


async def _emit(run_id: str, node: str, status: str, detail: str) -> None:
    metrics["node_events"] += 1
    await bus.emit(run_id, node, status, detail)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # RESUME: any run that was mid-flight when the process died restarts from its checkpoint
    for row in await find_resumable():
        state: PipelineState = json.loads(row.state_json)
        if state.get("run_id"):
            asyncio.create_task(launch(state))
    yield


app = FastAPI(title="Multi-Agent Incident Analysis Suite", version="1.0.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/metrics")
async def get_metrics() -> dict:
    return metrics


@app.post("/v1/analyses", dependencies=[Depends(require_key)])
async def create_analysis(file: UploadFile = File(...)) -> dict:
    s = get_settings()
    data = await file.read()
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"file exceeds {s.max_upload_mb} MB limit")
    if not (file.filename or "").lower().endswith((".log", ".txt", ".json", ".gz")):
        raise HTTPException(400, "unsupported file type")
    if (file.filename or "").lower().endswith(".gz"):
        import gzip
        data = gzip.decompress(data)
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        raise HTTPException(400, "file is not text-decodable")

    run_id = await create_run(file.filename or "upload.log")
    await audit(run_id, "upload", f"{file.filename} ({len(data)} bytes)")
    state: PipelineState = {
        "run_id": run_id, "filename": file.filename or "upload.log", "raw_text": text,
        "issues": [], "remediations": [], "jira": [], "errors": [], "nodes": {},
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "approved": None,
    }
    await save_checkpoint(run_id, dict(state), {}, "running")
    asyncio.create_task(launch(state))
    return {"id": run_id, "status": "running"}


@app.get("/v1/analyses", dependencies=[Depends(require_key)])
async def get_runs() -> list[dict]:
    return [{"id": r.id, "status": r.status, "filename": r.filename,
             "created_at": r.created_at.isoformat(), "cost_usd": r.cost_usd}
            for r in await list_runs()]


@app.get("/v1/analyses/{run_id}", dependencies=[Depends(require_key)])
async def get_run(run_id: str) -> RunStatus:
    row = await load_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    st = json.loads(row.state_json)
    return RunStatus(
        id=row.id, status=row.status, nodes=json.loads(row.nodes_json),
        issues=st.get("issues", []), remediations=st.get("remediations", []),
        cookbook_md=st.get("cookbook_md"), slack=st.get("slack"), jira=st.get("jira", []),
        errors=st.get("errors", []), tokens_in=row.tokens_in, tokens_out=row.tokens_out,
        cost_usd=row.cost_usd, filename=row.filename, created_at=row.created_at.isoformat())


@app.delete("/v1/analyses/{run_id}", dependencies=[Depends(require_key)])
async def delete_analysis(run_id: str) -> dict:
    row = await load_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    if row.status in ("queued", "running", "waiting_approval"):
        raise HTTPException(409, "cannot delete a run that is still in progress")
    await delete_run(run_id)
    return {"ok": True, "deleted": run_id}


@app.get("/v1/analyses/{run_id}/evidence", dependencies=[Depends(require_key)])
async def get_evidence(run_id: str, start: int, end: int) -> dict:
    row = await load_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    st = json.loads(row.state_json)
    lines = st.get("raw_text", "").splitlines()
    lo, hi = max(1, start - 3), min(len(lines), end + 3)
    return {"start": lo, "lines": lines[lo - 1: hi], "highlight": [start, end]}


@app.post("/v1/analyses/{run_id}/actions/approve", dependencies=[Depends(require_key)])
async def approve(run_id: str, body: ApproveAction) -> dict:
    row = await load_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    st = json.loads(row.state_json)
    st["approved"] = body.approve
    await save_checkpoint(run_id, st, json.loads(row.nodes_json), row.status)
    await audit(run_id, "approval", f"approved={body.approve}", actor="user")
    return {"ok": True, "approved": body.approve}


@app.get("/v1/analyses/{run_id}/events")
async def sse(run_id: str, request: Request) -> StreamingResponse:
    q = bus.subscribe(run_id)

    async def gen():
        try:
            # replay current node state first
            row = await load_run(run_id)
            if row:
                yield f"data: {json.dumps({'run_id': run_id, 'node': 'snapshot', 'status': row.status, 'detail': row.nodes_json})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(evt)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---- static UI ----
WEB = Path(__file__).resolve().parent.parent / "web"


@app.get("/")
async def index(request: Request) -> FileResponse:
    resp = FileResponse(WEB / "index.html")
    resp.set_cookie(
        SESSION_COOKIE,
        _make_session_token(get_settings().api_key),
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@app.exception_handler(Exception)
async def problem_json(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, media_type="application/problem+json",
                        content={"title": "Internal error", "detail": str(exc)[:300]})
