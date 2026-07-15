"""Supabase client wrapper — auth, retries, and all table operations.

Every call goes through `_safe()`: network drops or auth expiry must never
crash the worker loop (mirrors the old worker's tolerance of state.json
contention). On auth errors we re-sign-in once and retry.
"""
import logging
import time
from datetime import datetime, timezone

from supabase import create_client

log = logging.getLogger("worker")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Cloud:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = create_client(cfg.supabase_url, cfg.anon_key)
        self._signed_in = False

    # ── auth ────────────────────────────────────────────────
    def sign_in(self):
        self.client.auth.sign_in_with_password(
            {"email": self.cfg.email, "password": self.cfg.password}
        )
        self._signed_in = True
        log.info("Signed in to Supabase as worker %s", self.cfg.worker_id)

    def _safe(self, fn, what: str, default=None):
        """Run a supabase call; on failure try one re-auth + retry, else default."""
        for attempt in (1, 2):
            try:
                if not self._signed_in:
                    self.sign_in()
                return fn()
            except Exception as e:
                msg = str(e)
                log.debug("%s failed (attempt %d): %s", what, attempt, msg)
                if attempt == 1:
                    self._signed_in = False
                    time.sleep(1)
        return default

    # ── workers ─────────────────────────────────────────────
    def heartbeat(self, status: str, rk: dict, app_version: str):
        self._safe(
            lambda: self.client.table("workers").update({
                "last_seen": utcnow(),
                "status": status,
                "rk_open": rk.get("open", False),
                "rk_running": rk.get("running", False),
                "rk_scenario": rk.get("scenario"),
                "app_version": app_version,
            }).eq("id", self.cfg.worker_id).execute(),
            "heartbeat",
        )

    def set_status(self, status: str):
        self._safe(
            lambda: self.client.table("workers").update(
                {"status": status, "last_seen": utcnow()}
            ).eq("id", self.cfg.worker_id).execute(),
            "set_status",
        )

    def worker_enabled(self) -> bool:
        res = self._safe(
            lambda: self.client.table("workers").select("enabled")
                .eq("id", self.cfg.worker_id).single().execute(),
            "worker_enabled",
        )
        if res is None:
            return True  # network blip — keep working
        return bool(res.data.get("enabled", True))

    # ── tasks ───────────────────────────────────────────────
    def claim_next_task(self) -> dict | None:
        """Atomically claim the oldest pending task for this worker."""
        res = self._safe(
            lambda: self.client.table("tasks").select("*")
                .eq("worker_id", self.cfg.worker_id)
                .eq("status", "pending")
                .order("created_at").limit(1).execute(),
            "next_pending",
        )
        if not res or not res.data:
            return None
        task = res.data[0]
        claimed = self._safe(
            lambda: self.client.table("tasks").update({
                "status": "running", "started_at": utcnow(),
            }).eq("id", task["id"]).eq("status", "pending").execute(),
            "claim_task",
        )
        if claimed and claimed.data:
            return claimed.data[0] | {"scenario_name": task["scenario_name"],
                                       "scenario_path": task.get("scenario_path")}
        return None

    def update_task(self, task_id: str, fields: dict):
        self._safe(
            lambda: self.client.table("tasks").update(fields)
                .eq("id", task_id).execute(),
            "update_task",
        )

    def insert_external_task(self, scenario_name: str) -> str | None:
        res = self._safe(
            lambda: self.client.table("tasks").insert({
                "scenario_name": scenario_name,
                "worker_id": self.cfg.worker_id,
                "status": "running",
                "source": "external",
                "started_at": utcnow(),
            }).execute(),
            "insert_external_task",
        )
        return res.data[0]["id"] if res and res.data else None

    def get_task(self, task_id: str) -> dict | None:
        res = self._safe(
            lambda: self.client.table("tasks").select("*")
                .eq("id", task_id).single().execute(),
            "get_task",
        )
        return res.data if res and res.data else None

    def task_status(self, task_id: str) -> str | None:
        res = self._safe(
            lambda: self.client.table("tasks").select("status")
                .eq("id", task_id).single().execute(),
            "task_status",
        )
        return res.data.get("status") if res and res.data else None

    # ── logs ────────────────────────────────────────────────
    def insert_logs(self, rows: list[dict]):
        if not rows:
            return
        self._safe(
            lambda: self.client.table("task_logs").insert(rows).execute(),
            "insert_logs",
        )

    # ── commands ────────────────────────────────────────────
    def pending_commands(self) -> list[dict]:
        res = self._safe(
            lambda: self.client.table("commands").select("*")
                .eq("worker_id", self.cfg.worker_id)
                .eq("status", "pending")
                .order("created_at").execute(),
            "pending_commands",
        )
        return res.data if res and res.data else []

    def ack_command(self, cmd_id: str):
        self._safe(
            lambda: self.client.table("commands").update(
                {"status": "acked", "acked_at": utcnow()}
            ).eq("id", cmd_id).execute(),
            "ack_command",
        )

    def finish_command(self, cmd_id: str, ok: bool = True, result: dict | None = None):
        self._safe(
            lambda: self.client.table("commands").update({
                "status": "done" if ok else "failed",
                "finished_at": utcnow(),
                "result": result or {},
            }).eq("id", cmd_id).execute(),
            "finish_command",
        )

    # ── releases (auto-update) ──────────────────────────────
    def latest_release(self) -> dict | None:
        res = self._safe(
            lambda: self.client.table("worker_releases").select("*")
                .order("released_at", desc=True).limit(1).execute(),
            "latest_release",
        )
        return res.data[0] if res and res.data else None

    def download_release(self, storage_path: str) -> bytes | None:
        return self._safe(
            lambda: self.client.storage.from_("worker-releases").download(storage_path),
            "download_release",
        )

    # ── windows scheduler snapshot ──────────────────────────
    def replace_win_tasks(self, rows: list[dict]):
        self._safe(
            lambda: self.client.table("win_tasks").delete()
                .eq("worker_id", self.cfg.worker_id).execute(),
            "clear_win_tasks",
        )
        if rows:
            self._safe(
                lambda: self.client.table("win_tasks").insert(rows).execute(),
                "insert_win_tasks",
            )

    def enqueue_task(self, scenario_name: str) -> bool:
        """Insert a cloud task for THIS worker (used by --enqueue from a
        Windows scheduled task). Returns True on success."""
        res = self._safe(
            lambda: self.client.table("tasks").insert({
                "scenario_name": scenario_name,
                "worker_id": self.cfg.worker_id,
                "source": "win_schedule",
            }).execute(),
            "enqueue_task",
        )
        return bool(res and res.data)

    # ── scenarios ───────────────────────────────────────────
    def upsert_scenarios(self, entries: list[dict]):
        """entries: [{name, path?}] — rows without 'path' leave any existing
        custom path untouched (PostgREST only updates supplied columns)."""
        now = utcnow()
        plain  = [e for e in entries if "path" not in e]
        pathed = [e for e in entries if "path" in e]
        for batch in (plain, pathed):
            if not batch:
                continue
            rows = [{**e, "reported_by": self.cfg.worker_id, "last_seen_at": now}
                    for e in batch]
            self._safe(
                lambda rows=rows: self.client.table("scenarios")
                    .upsert(rows, on_conflict="name").execute(),
                "upsert_scenarios",
            )
