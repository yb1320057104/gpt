"""Isolated GPT-Register-Tool protocol-registration sidecar."""
from __future__ import annotations
import asyncio, json, sys, time, uuid
from pathlib import Path

class ProtocolRegistrationService:
    def __init__(self, app_root: Path, importer=None):
        self.root = app_root.parent.parent / "protocol-registration"
        self.script = self.root / "chatgpt_phone_reg.py"
        self.jobs: dict[str, dict] = {}
        self.importer = importer
        # Session files are durable sidecar artifacts.  Keep an in-process
        # index as well so a watcher restart/re-scan cannot import the same
        # session twice into the normal account pool.
        self._imported_sessions: set[str] = set()
        self._imported_emails: set[str] = set()

    async def start(self, *, count=1, workers=1, mailbox_file="", proxy="", proxy_pool=None):
        if not self.script.is_file():
            raise RuntimeError(f"协议注册 sidecar 不存在: {self.script}")
        job_id = uuid.uuid4().hex
        args = [sys.executable, str(self.script), "--count", str(max(1, int(count))), "--workers", str(max(1, int(workers)))]
        if mailbox_file: args += ["--mailbox-file", mailbox_file]
        pool = []
        for value in (proxy_pool or []):
            value = str(value or "").strip()
            if value and value not in pool:
                pool.append(value)
        if proxy and proxy not in pool:
            pool.insert(0, proxy)
        if pool:
            # The sidecar accepts newline/comma separated values and performs
            # per-account rotation/retry itself.  Do not collapse this to the
            # first candidate.
            args += ["--proxy-pool", "\n".join(pool)]
        elif proxy:
            args += ["--proxy", proxy]
        proc = await asyncio.create_subprocess_exec(*args, cwd=str(self.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.jobs[job_id] = {"id": job_id, "status": "running", "pid": proc.pid, "lines": [], "startedAt": time.time(), "startedWall": time.time(), "proxyPoolSize": len(pool)}
        asyncio.create_task(self._watch(job_id, proc))
        return {"jobId": job_id, "status": "running", "pid": proc.pid}

    async def _watch(self, job_id, proc):
        state = self.jobs[job_id]
        async for raw in proc.stdout:
            state["lines"] = (state["lines"] + [raw.decode("utf-8", "replace").rstrip()])[-200:]
        code = await proc.wait(); state["status"] = "completed" if code == 0 else "failed"; state["exitCode"] = code
        if code == 0 and self.importer:
            imported = 0
            started_wall = float(state.get("startedWall", 0)) - 5
            for path in sorted((self.root / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
                try:
                    session_key = str(path.resolve()).lower()
                    if session_key in self._imported_sessions:
                        continue
                    if path.stat().st_mtime < started_wall:
                        continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                    email = str(data.get("email") or "").strip().lower()
                    if data.get("success") is True and email and email not in self._imported_emails:
                        await self.importer(data); imported += 1
                        self._imported_sessions.add(session_key)
                        self._imported_emails.add(email)
                except Exception:
                    continue
            state["importedAccounts"] = imported

    def get(self, job_id):
        return self.jobs.get(job_id)
