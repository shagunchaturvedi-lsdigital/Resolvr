"""LLM client abstraction.

- Live mode: any OpenAI-compatible Chat Completions endpoint (OpenRouter, Gemini,
  OpenAI, etc. — swap via IAS_LLM_BASE_URL/IAS_LLM_API_KEY/IAS_MODEL) with
  structured JSON output.
- Mock mode: deterministic rule-based outputs (CI + offline demo fallback).
Every call: token accounting, timeout, one schema-repair retry.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..schemas import (Category, ClassifierIssueOut, ClassifierOut, CookbookOut,
                       RemediationOut, Severity)

T = TypeVar("T", bound=BaseModel)

# gpt-4o-mini pricing via OpenRouter (USD per 1M tokens) — used for per-run cost display.
# Approximate: actual cost varies by the model configured in IAS_MODEL.
PRICE_IN, PRICE_OUT = 0.15, 0.60


class TokenMeter:
    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0

    def add(self, i: int, o: int) -> None:
        self.tokens_in += i
        self.tokens_out += o

    @property
    def cost_usd(self) -> float:
        return round(self.tokens_in / 1e6 * PRICE_IN + self.tokens_out / 1e6 * PRICE_OUT, 4)


def _extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    return text[start:]


class LLMClient:
    def __init__(self, meter: TokenMeter):
        self.settings = get_settings()
        self.meter = meter
        self._client = None
        if self.settings.llm_is_live:
            import ssl

            import httpx
            import openai
            import truststore

            # Trust the OS certificate store (needed behind TLS-inspecting corporate proxies).
            ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self._client = openai.AsyncOpenAI(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
                http_client=httpx.AsyncClient(verify=ssl_context),
            )

    async def structured(self, system: str, user: str, schema: Type[T], max_tokens: int = 2000) -> T:
        """Call LLM, parse into schema. Up to two auto-repair retries, then raise."""
        if not self.settings.llm_is_live:
            return _mock_response(system, user, schema)

        last_err: Exception | None = None
        prompt = user
        extra_body: dict = {}
        if "openrouter.ai" in self.settings.llm_base_url:
            # Request the least-restrictive data policy so an unconfigured account-wide
            # privacy setting doesn't 404 every call with "No endpoints available matching
            # your guardrail restrictions and data policy."
            extra_body["provider"] = {"data_collection": "allow"}
        if "generativelanguage.googleapis.com" in self.settings.llm_base_url and "flash" in self.settings.model and "pro" not in self.settings.model:
            # Gemini Flash spends part of max_tokens on invisible "thinking" tokens before
            # writing the visible response — on a big log chunk this can eat the whole
            # budget and truncate the JSON output mid-string. Not needed for classification/
            # remediation tasks, and can't be disabled on Pro/3.x models, hence the model check.
            extra_body["reasoning_effort"] = "none"
        for attempt in range(3):
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.settings.model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system + "\nRespond with ONLY valid JSON matching the required schema. No prose, no markdown fences."},
                        {"role": "user", "content": prompt},
                    ],
                    extra_body=extra_body,
                ),
                timeout=self.settings.node_timeout_s,
            )
            self.meter.add(resp.usage.prompt_tokens, resp.usage.completion_tokens)
            raw = resp.choices[0].message.content or ""
            try:
                return schema.model_validate_json(_extract_json(raw))
            except (ValidationError, json.JSONDecodeError) as e:
                last_err = e
                prompt = (f"{user}\n\nYour previous output failed validation with: {e}\n"
                          f"Previous output was: {raw[:2000]}\nReturn corrected JSON only.")
        raise ValueError(f"LLM output failed schema validation after retries: {last_err}")


# ---------------------------------------------------------------------------
# Deterministic mock — rule-based classification so CI/offline demos are real
# ---------------------------------------------------------------------------
_RULES = [
    (r"ECONNREFUSED|connection refused|Connection to .* refused", Category.dependency, Severity.critical,
     "Repeated connection refusals to downstream dependency"),
    (r"OOMKilled|Out of memory|oom-killer", Category.infrastructure, Severity.high,
     "Container/process killed due to out-of-memory"),
    (r"certificate .*expire|x509: certificate", Category.security, Severity.high,
     "TLS certificate expired or invalid"),
    (r"502 Bad Gateway|upstream prematurely closed", Category.dependency, Severity.medium,
     "Upstream 502 errors from gateway"),
    (r"deprecated", Category.configuration, Severity.low,
     "Deprecated configuration option in use"),
    (r"Traceback|Exception|ERROR", Category.app_error, Severity.medium,
     "Unhandled application error"),
]

_FIXES = {
    Category.dependency: ("Downstream service unreachable — likely crashed pod or exhausted connection pool.",
                          "1. `kubectl get pods -n <ns>` — check target service status\n2. Inspect service endpoints: `kubectl get endpoints`\n3. Restart the unhealthy pod and verify connection pool limits\n4. Confirm recovery: error rate returns to baseline",
                          "Connection refusals in bursts indicate the listener is down, not a network partition.", "low"),
    Category.infrastructure: ("Workload exceeded its memory limit and was OOMKilled.",
                              "1. `kubectl describe pod <pod>` — confirm OOMKilled in last state\n2. Review memory limit vs. actual usage (`kubectl top pod`)\n3. Raise the limit or fix the leak; redeploy\n4. Add a memory alert at 85% of limit",
                              "OOMKilled events correlate with the restart timestamps in the log.", "medium"),
    Category.security: ("A TLS certificate in the chain has expired.",
                        "1. Identify the cert: `openssl s_client -connect host:443 | openssl x509 -enddate -noout`\n2. Renew via your CA / cert-manager\n3. Roll the secret and restart consumers\n4. Add expiry monitoring (30-day alert)",
                        "x509 validation errors began exactly at the expiry timestamp.", "low"),
    Category.app_error: ("Unhandled exception in application code path.",
                         "1. Locate the stack trace top frame\n2. Reproduce with the logged input\n3. Patch and add a regression test\n4. Deploy and monitor error rate",
                         "Stack traces share an identical top frame, indicating a single code defect.", "medium"),
    Category.configuration: ("Deprecated configuration will break on next upgrade.",
                             "1. Consult upgrade notes for the replacement key\n2. Update config in IaC, not by hand\n3. Validate in staging before rollout",
                             "Warning is non-breaking today but blocks the next version upgrade.", "low"),
}


def _mock_response(system: str, user: str, schema: Type[T]) -> T:
    if schema is ClassifierOut:
        issues: list[ClassifierIssueOut] = []
        seen: set[str] = set()
        for idx, line in enumerate(user.splitlines(), start=1):
            for pat, cat, sev, summary in _RULES:
                if re.search(pat, line, re.I):
                    sig = re.sub(r"^\S+\s+\S+\s*", "", line)[:120] or line[:120]
                    key = f"{cat}:{summary}"
                    if key in seen:
                        for it in issues:
                            if it.category == cat and it.summary == summary:
                                it.occurrences += 1
                                it.end_line = max(it.end_line, idx)
                        break
                    seen.add(key)
                    issues.append(ClassifierIssueOut(
                        category=cat, severity=sev, confidence=0.9, summary=summary,
                        error_signature=sig, start_line=idx, end_line=idx))
                    break
        return ClassifierOut(issues=issues)  # type: ignore[return-value]
    if schema is RemediationOut:
        for cat, (rc, fix, why, risk) in _FIXES.items():
            if cat.value.lower() in user.lower():
                return RemediationOut(root_cause=rc, fix_steps_md=fix, rationale=why, risk=risk,
                                      citations=[f"runbook:{cat.name}-standard"])  # type: ignore[return-value]
        return RemediationOut(root_cause="Undetermined — requires manual review",
                              fix_steps_md="1. Inspect surrounding log context\n2. Escalate to service owner",
                              rationale="Insufficient signal for automated diagnosis.", risk="low")  # type: ignore[return-value]
    if schema is CookbookOut:
        return CookbookOut(content_md=_mock_cookbook(user))  # type: ignore[return-value]
    raise ValueError(f"Mock has no handler for schema {schema}")


def _mock_cookbook(context: str) -> str:
    return (
        "# Incident Response Checklist\n\n## Immediate Actions\n"
        "- [ ] Acknowledge the page and open an incident channel\n"
        "- [ ] Address Critical issues first (see ranked list in analysis)\n"
        "- [ ] Restore the failed dependency before restarting consumers\n\n"
        "## Verification\n- [ ] Error rate back to baseline for 15 min\n- [ ] All pods Ready, no restarts in 10 min\n\n"
        "## Prevention\n- [ ] Add alerting for the detected failure signatures\n- [ ] Fix deprecated configuration before next upgrade\n- [ ] Add cert expiry monitoring\n\n"
        "## Escalate If\n- [ ] Errors persist 30 min after remediation\n- [ ] Data integrity is in question\n"
    )
