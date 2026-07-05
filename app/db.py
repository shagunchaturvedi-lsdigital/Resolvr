"""Persistence: runs, audit log, and pipeline checkpoints (resumable runs)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer, Float, select, delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    filename: Mapped[str] = mapped_column(String(255), default="")
    state_json: Mapped[str] = mapped_column(Text, default="{}")   # checkpointed pipeline state
    nodes_json: Mapped[str] = mapped_column(Text, default="{}")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    actor: Mapped[str] = mapped_column(String(64), default="api")
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


engine = create_async_engine(get_settings().database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def create_run(filename: str) -> str:
    run_id = str(uuid.uuid4())
    async with SessionLocal() as s:
        s.add(RunRow(id=run_id, filename=filename, status="queued"))
        await s.commit()
    return run_id


async def save_checkpoint(run_id: str, state: dict, nodes: dict, status: str) -> None:
    async with SessionLocal() as s:
        row = await s.get(RunRow, run_id)
        if not row:
            return
        row.state_json = json.dumps(state, default=str)
        row.nodes_json = json.dumps(nodes)
        row.status = status
        row.tokens_in = state.get("tokens_in", 0)
        row.tokens_out = state.get("tokens_out", 0)
        row.cost_usd = state.get("cost_usd", 0.0)
        if status in ("complete", "partial", "failed", "budget_exceeded"):
            row.completed_at = datetime.utcnow()
        await s.commit()


async def load_run(run_id: str) -> RunRow | None:
    async with SessionLocal() as s:
        return await s.get(RunRow, run_id)


async def list_runs(limit: int = 50) -> list[RunRow]:
    async with SessionLocal() as s:
        rows = await s.execute(select(RunRow).order_by(RunRow.created_at.desc()).limit(limit))
        return list(rows.scalars())


async def delete_run(run_id: str) -> bool:
    async with SessionLocal() as s:
        row = await s.get(RunRow, run_id)
        if not row:
            return False
        await s.execute(delete(AuditRow).where(AuditRow.run_id == run_id))
        await s.delete(row)
        await s.commit()
        return True


async def find_resumable() -> list[RunRow]:
    """Runs that were mid-flight when the process died — resumed at startup."""
    async with SessionLocal() as s:
        rows = await s.execute(select(RunRow).where(RunRow.status.in_(["queued", "running"])))
        return list(rows.scalars())


async def audit(run_id: str, action: str, detail: str = "", actor: str = "api") -> None:
    async with SessionLocal() as s:
        s.add(AuditRow(run_id=run_id, action=action, detail=detail[:4000], actor=actor))
        await s.commit()
