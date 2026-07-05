"""External integrations. Thin, idempotent adapters.

If credentials are absent the adapters run in dry-run mode and return a
simulated payload — the pipeline and UI behave identically, so the demo
works with or without live tokens (and CI never needs secrets).
"""
from __future__ import annotations

import ssl

import httpx
import truststore

from ..config import get_settings

# Use the OS-native certificate trust store rather than the bundled certifi CAs.
# Needed on machines behind a TLS-inspecting corporate proxy, whose root CA is
# trusted by Windows/macOS/Linux but not by certifi.
_SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

_SEV_EMOJI = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢"}


def build_slack_blocks(run_id: str, issues: list[dict], filename: str | None, run_url: str) -> list[dict]:
    top = sorted(issues, key=lambda i: ["Critical", "High", "Medium", "Low"].index(i["severity"]))[:5]
    lines = "\n".join(
        f"{_SEV_EMOJI[i['severity']]} *{i['severity']}* — {i['summary']} (×{i['count']})" for i in top
    )
    label = filename or run_id[:8]
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "🚨 Incident Analysis Complete"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*<{run_url}|{label}>*\n{lines or 'No issues detected.'}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Run `{run_id}` · Multi-Agent Incident Suite"}]},
    ]


async def post_slack(run_id: str, issues: list[dict], filename: str | None = None) -> dict:
    s = get_settings()
    run_url = f"{s.public_base_url}/?run={run_id}"
    blocks = build_slack_blocks(run_id, issues, filename, run_url)
    if not s.slack_bot_token or not s.slack_channel:
        return {"ok": True, "dry_run": True, "channel": s.slack_channel or "(unconfigured)",
                "blocks": blocks, "detail": "Slack not configured — dry-run payload generated"}
    async with httpx.AsyncClient(timeout=15, verify=_SSL_CONTEXT) as client:
        for attempt in range(3):
            r = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {s.slack_bot_token}"},
                json={"channel": s.slack_channel, "blocks": blocks,
                      "text": f"Incident analysis complete for run {run_id}"},
            )
            if r.status_code == 429:
                import asyncio
                await asyncio.sleep(int(r.headers.get("Retry-After", 2)) * (attempt + 1))
                continue
            data = r.json()
            return {"ok": data.get("ok", False), "dry_run": False, "ts": data.get("ts"),
                    "channel": s.slack_channel, "detail": data.get("error", "posted")}
    return {"ok": False, "dry_run": False, "detail": "rate-limited after retries"}


async def create_jira_ticket(run_id: str, issue: dict, remediation: dict | None) -> dict:
    s = get_settings()
    fp = issue["fingerprint"]
    run_url = f"{s.public_base_url}/?run={run_id}"
    summary = f"[Auto] {issue['severity']}: {issue['summary']}"
    description = (
        f"*Category:* {issue['category']}  *Occurrences:* {issue['count']}\n"
        f"*Evidence:* {issue.get('evidence', [])}\n\n"
        + (f"*Root cause:* {remediation['root_cause']}\n\n*Fix:*\n{remediation['fix_steps_md']}\n\n"
           f"*Rationale:* {remediation['rationale']}" if remediation else "")
    )
    description_doc = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Detected by Multi-Agent Incident Suite — "},
            {"type": "text", "text": f"run {run_id}",
             "marks": [{"type": "link", "attrs": {"href": run_url}}]},
        ]},
        {"type": "paragraph", "content": [{"type": "text", "text": description[:30000]}]},
    ]}
    if not (s.jira_base_url and s.jira_email and s.jira_api_token):
        return {"ok": True, "dry_run": True, "key": f"{s.jira_project_key}-DRY-{fp[:6]}",
                "summary": summary, "detail": "JIRA not configured — dry-run ticket generated"}

    auth = (s.jira_email, s.jira_api_token)
    async with httpx.AsyncClient(timeout=20, auth=auth, verify=_SSL_CONTEXT) as client:
        # duplicate suppression: open ticket already labeled with this fingerprint?
        # NB: the legacy GET /rest/api/3/search endpoint was removed by Atlassian (410 Gone) —
        # https://developer.atlassian.com/changelog/#CHANGE-2046 — this uses its replacement.
        jql = f'project = {s.jira_project_key} AND labels = "fp-{fp}" AND statusCategory != Done'
        dup = await client.post(f"{s.jira_base_url}/rest/api/3/search/jql",
                                json={"jql": jql, "maxResults": 1, "fields": ["key"]})
        if dup.status_code == 200 and dup.json().get("issues"):
            key = dup.json()["issues"][0]["key"]
            return {"ok": True, "dry_run": False, "key": key, "duplicate": True,
                    "detail": f"Open ticket {key} already tracks this fingerprint"}
        r = await client.post(
            f"{s.jira_base_url}/rest/api/3/issue",
            json={"fields": {
                "project": {"key": s.jira_project_key},
                "issuetype": {"name": s.jira_issue_type},
                "summary": summary[:250],
                "labels": ["auto-generated", f"fp-{fp}"],
                "description": description_doc,
            }},
        )
        if r.status_code == 201:
            return {"ok": True, "dry_run": False, "key": r.json()["key"], "summary": summary, "detail": "created"}
        return {"ok": False, "dry_run": False, "detail": f"JIRA error {r.status_code}: {r.text[:300]}"}
