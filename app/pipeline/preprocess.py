"""Pre-LLM utilities: secret redaction, chunking, fingerprinting."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

REDACTION_RULES: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")),
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("BEARER", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_\.=]{16,}")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("IPV4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b")


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    freq = {c: s.count(c) for c in set(s)}
    return -sum((n / len(s)) * math.log2(n / len(s)) for n in freq.values())


def redact(text: str) -> tuple[str, int]:
    """Mask secrets/PII before any LLM call. Returns (masked_text, n_redactions)."""
    n = 0
    for label, pat in REDACTION_RULES:
        text, k = pat.subn(f"[REDACTED:{label}]", text)
        n += k

    def _maybe(m: re.Match) -> str:
        nonlocal n
        tok = m.group(0)
        if _entropy(tok) > 4.2:
            n += 1
            return "[REDACTED:SECRET]"
        return tok

    text = _HIGH_ENTROPY.sub(_maybe, text)
    return text, n


@dataclass
class Chunk:
    file: str
    start_line: int  # 1-based inclusive
    end_line: int
    text: str


def chunk_lines(filename: str, text: str, size: int = 200, overlap: int = 20) -> list[Chunk]:
    lines = text.splitlines()
    if not lines:
        return []
    chunks: list[Chunk] = []
    step = max(1, size - overlap)
    i = 0
    while i < len(lines):
        seg = lines[i : i + size]
        chunks.append(Chunk(file=filename, start_line=i + 1, end_line=i + len(seg), text="\n".join(seg)))
        if i + size >= len(lines):
            break
        i += step
    return chunks


_NORMALIZERS = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?", re.I), "<TS>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b\d+\b"), "<N>"),
    (re.compile(r"\s+"), " "),
]


def fingerprint(signature: str) -> str:
    """Stable hash of a normalized error signature — dedupe key across chunks/runs."""
    s = signature.strip().lower()
    for pat, repl in _NORMALIZERS:
        s = pat.sub(repl, s)
    return hashlib.sha256(s.encode()).hexdigest()[:16]
