"""Tell you when a job needs you, so you don't have to keep the dashboard open.

Channels (configure any, all optional, in .env):
* Synology Chat — an incoming webhook URL from the Chat app (Integration → Incoming Webhooks)
* LINE — Messaging API channel access token + your user / group ID (LINE Notify no longer exists)
* Any other service — a generic webhook that receives JSON
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime

from .config import Settings
from .jobtypes import JOB_TYPES

log = logging.getLogger("assistant.notify")
TIMEOUT = 10


class Notifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def channels(self) -> list[str]:
        s = self.settings
        found = []
        if s.notify_synology_url:
            found.append("synology")
        if s.notify_line_token and s.notify_line_to:
            found.append("line")
        if s.notify_webhook_url:
            found.append("webhook")
        return found

    def job_url(self, job_id: str) -> str:
        base = self.settings.dashboard_url.rstrip("/")
        return f"{base}/#/job/{job_id}" if base else ""

    def job_text(self, job: dict, event: str) -> str:
        job_type = JOB_TYPES.get(job.get("type"))
        label = job_type.label if job_type else job.get("type", "")
        runs = len(job.get("runs") or [])
        lines = {
            "needs_review": [f"งานรอตรวจ: {job['title']}", f"{label} · รอบที่ {runs}"],
            "failed": [f"งานไม่สำเร็จ: {job['title']}", str(job.get("error") or "")[:300]],
            "quota_wait": [f"โควต้า subscription เต็ม: {job['title']}", str(job.get("error") or "")],
        }.get(event, [job["title"]])
        url = self.job_url(job["id"])
        return "\n".join([x for x in lines if x] + ([url] if url else []))

    async def job_event(self, job: dict, event: str) -> dict:
        if event not in self.settings.notify_on or not self.channels:
            return {}
        payload = {"event": event, "job": {"id": job["id"], "title": job["title"], "type": job.get("type"),
                                           "status": job.get("status"), "url": self.job_url(job["id"])}}
        return await self.send(self.job_text(job, event), payload)

    async def send(self, text: str, payload: dict | None = None) -> dict:
        """Send to every configured channel; one failing channel never blocks the others or the job."""
        payload = {**(payload or {}), "text": text, "sent_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        jobs = {name: asyncio.to_thread(getattr(self, f"_send_{name}"), text, payload) for name in self.channels}
        results = {}
        for name, coro in jobs.items():
            try:
                await coro
                results[name] = "ok"
            except Exception as exc:  # network, auth, bad URL ...
                log.warning("notify via %s failed: %s", name, exc)
                results[name] = f"error: {exc}"
        return results

    # -- channels ------------------------------------------------------------
    @staticmethod
    def _post(url: str, data: bytes, headers: dict) -> None:
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}")

    def _send_synology(self, text: str, payload: dict) -> None:
        body = urllib.parse.urlencode({"payload": json.dumps({"text": text}, ensure_ascii=False)}).encode()
        self._post(self.settings.notify_synology_url, body, {"Content-Type": "application/x-www-form-urlencoded"})

    def _send_line(self, text: str, payload: dict) -> None:
        body = json.dumps({"to": self.settings.notify_line_to, "messages": [{"type": "text", "text": text[:4900]}]},
                          ensure_ascii=False).encode()
        self._post(self.settings.notify_line_api, body, {"Content-Type": "application/json",
                                                         "Authorization": f"Bearer {self.settings.notify_line_token}"})

    def _send_webhook(self, text: str, payload: dict) -> None:
        self._post(self.settings.notify_webhook_url, json.dumps(payload, ensure_ascii=False).encode(),
                   {"Content-Type": "application/json"})
