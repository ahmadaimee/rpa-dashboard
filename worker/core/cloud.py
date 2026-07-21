"""Supabase client wrapper — auth, retries, and all table operations.

Every call goes through `_safe()`: network drops or auth expiry must never
crash the worker loop (mirrors the old worker's tolerance of state.json
contention). On auth errors we re-sign-in once and retry.
"""
import logging
import threading
import time
from datetime import datetime, timezone

from supabase import create_client

log = logging.getLogger("worker")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Cloud:
    """All Supabase access goes through _safe(): serialized with a lock
    (supabase-py's sync client is not thread-safe — heartbeat/commands/main
    threads racing on it caused constant failures), with throttled,
    backing-off password sign-ins (the old retry-with-relogin pattern
    hammered /auth/v1/token every couple of seconds → 429 storms that
    also broke update downloads)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = create_client(cfg.supabase_url, cfg.anon_key)
        self._signed_in = False
        self._lock = threading.RLock()
        self._signin_at = 0.0        # monotonic time of last sign-in attempt
        self._signin_backoff = 0.0   # grows on failures/429s
        self._consec_failures = 0    # self-heal restart when the client is wedged
        self._last_error = ""
        self._company = None         # this worker's company slug (lazy)
        self._company_loaded = False

    # ── auth ────────────────────────────────────────────────
    def sign_in(self):
        with self._lock:
            self._sign_in_locked()

    def _sign_in_locked(self):
        wait = self._signin_backoff - (time.monotonic() - self._signin_at)
        if wait > 0:
            time.sleep(min(wait, 30))
        self._signin_at = time.monotonic()
        try:
            self.client.auth.sign_in_with_password(
                {"email": self.cfg.email, "password": self.cfg.password}
            )
        except Exception:
            # exponential backoff — never hammer the auth endpoint again
            self._signin_backoff = min(max(self._signin_backoff, 5) * 2, 120)
            raise
        self._signin_backoff = 10   # even successes are rate-limited
        self._signed_in = True
        log.info("Signed in to Supabase as worker %s", self.cfg.worker_id)

    def _safe(self, fn, what: str, default=None):
        """Run a supabase call; on failure try one re-auth + retry, else default."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if not self._signed_in:
                        self._sign_in_locked()
                    result = fn()
                    self._consec_failures = 0
                    return result
                except Exception as e:
                    self._last_error = str(e)
                    log.warning("%s failed (attempt %d): %s",
                                what, attempt, str(e)[:200])
                    if attempt == 1:
                        self._signed_in = False
                        time.sleep(1)
            self._consec_failures += 1
            # Restart only recovers a wedged process (e.g. PyInstaller temp
            # dir deleted → missing cacert). A plain network outage must NOT
            # trigger restart loops — we just keep retrying.
            wedged = "No such file or directory" in (self._last_error or "")
            if (wedged and self._consec_failures >= 30) or self._consec_failures >= 400:
                self._self_heal()
            return default

    def _self_heal(self):
        """The client is wedged (e.g. PyInstaller temp dir deleted from under
        us) — restart the whole worker; a fresh process fully recovers."""
        import os
        log.error("30+ consecutive cloud failures — restarting worker to recover")
        try:
            from . import installer
            from .config import PID_FILE
            PID_FILE.unlink(missing_ok=True)
            installer.relaunch_background()
        except Exception as e:
            log.error("Self-heal relaunch failed: %s", e)
        os._exit(1)

    # ── workers ─────────────────────────────────────────────
    def heartbeat(self, status: str, rk: dict, app_version: str,
                  lic: dict | None = None):
        fields = {
            "last_seen": utcnow(),
            "status": status,
            "rk_open": rk.get("open", False),
            "rk_running": rk.get("running", False),
            "rk_scenario": rk.get("scenario"),
            "app_version": app_version,
        }
        if lic and lic.get("status") != "unknown":
            fields.update({
                "license_status": lic["status"],
                "license_last_verified": lic.get("last_verified"),
                "license_error": lic.get("error"),
            })
        self._safe(
            lambda: self.client.table("workers").update(fields)
                .eq("id", self.cfg.worker_id).execute(),
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

    def cleanup_orphan_running(self):
        """On startup: no task can genuinely be running in a brand-new
        process — close rows a previous process left behind (a worker
        restart mid-run used to leave a second 'running' row forever)."""
        self._safe(
            lambda: self.client.table("tasks").update({
                "status": "stopped",
                "error": "Worker restarted — run tracking was interrupted",
                "finished_at": utcnow(),
            }).eq("worker_id", self.cfg.worker_id)
              .eq("status", "running").execute(),
            "cleanup_orphan_running",
        )

    def close_other_running_external(self):
        """Keyence runs one automation at a time — before opening a new
        on-PC row, close any external row still marked running."""
        self._safe(
            lambda: self.client.table("tasks").update({
                "status": "stopped",
                "error": "Superseded — a new run started on this PC",
                "finished_at": utcnow(),
            }).eq("worker_id", self.cfg.worker_id)
              .eq("source", "external")
              .eq("status", "running").execute(),
            "close_other_running_external",
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
    def my_company(self) -> str | None:
        """This worker's company slug (scenarios are unique per company)."""
        if not self._company_loaded:
            res = self._safe(
                lambda: self.client.table("workers").select("company")
                    .eq("id", self.cfg.worker_id).single().execute(),
                "my_company",
            )
            if res is not None:
                self._company = (res.data or {}).get("company")
                self._company_loaded = True
        return self._company

    def company_folder(self) -> str | None:
        """The company's default scenarios folder (may contain {USERNAME})."""
        co = self.my_company()
        if not co:
            return None
        res = self._safe(
            lambda: self.client.table("companies").select("scenarios_folder")
                .eq("slug", co).single().execute(),
            "company_folder",
        )
        return (res.data or {}).get("scenarios_folder") if res else None

    def upsert_scenarios(self, entries: list[dict]):
        """entries: [{name, path?}] — rows without 'path' leave any existing
        custom path untouched (PostgREST only updates supplied columns)."""
        now = utcnow()
        company = self.my_company()
        plain  = [e for e in entries if "path" not in e]
        pathed = [e for e in entries if "path" in e]
        for batch in (plain, pathed):
            if not batch:
                continue
            rows = [{**e, "reported_by": self.cfg.worker_id,
                     "last_seen_at": now, "company": company}
                    for e in batch]
            self._safe(
                lambda rows=rows: self.client.table("scenarios")
                    .upsert(rows, on_conflict="company,name").execute(),
                "upsert_scenarios",
            )
