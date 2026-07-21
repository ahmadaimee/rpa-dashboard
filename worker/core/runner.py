"""Execute one Keyence RK scenario and stream its output to the cloud.

Port of the old run_scenario(): launches RkScenarioManager.exe /RUN, streams
stdout in batches to task_logs, waits until the engine fully exits (the
launcher returns before the engine finishes), and — unlike the old code —
sets success/failed from the launcher exit code.
"""
import logging
import os
import platform
import re
import subprocess
import threading
import time
from datetime import datetime

from .cloud import Cloud, utcnow

try:
    import keyboard as kb
except Exception:
    kb = None

log = logging.getLogger("worker")

LOG_FLUSH_LINES = 15
LOG_FLUSH_SECS  = 3
LOG_MAX_LINES   = 500

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0


def rk_engine_running() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq RkScenarioManager.exe", "/NH"],
            text=True, creationflags=CREATE_NO_WINDOW,
        )
        return "RkScenarioManager.exe" in out
    except Exception:
        return False


def send_ctrl_alt_p():
    """Keyence's global stop hotkey."""
    if kb:
        try:
            kb.send("ctrl+alt+p")
            log.info("Sent CTRL+ALT+P kill hotkey")
            return
        except Exception as e:
            log.warning("keyboard.send failed: %s", e)
    log.warning("keyboard module unavailable — CTRL+ALT+P not sent")


class LogBatcher:
    """Buffers log lines; flushes to task_logs every N lines / N seconds."""

    def __init__(self, cloud: Cloud, task_id: str):
        self.cloud = cloud
        self.task_id = task_id
        self.buf: list[str] = []
        self.seq = 0
        self.total = 0
        self.last_flush = time.monotonic()
        self.lock = threading.Lock()

    def add(self, line: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            if self.total >= LOG_MAX_LINES:
                return
            self.buf.append(f"[{ts}] {line}")
            self.total += 1
            if self.total == LOG_MAX_LINES:
                self.buf.append(f"[{ts}] … output capped at {LOG_MAX_LINES} lines")
            if (len(self.buf) >= LOG_FLUSH_LINES
                    or time.monotonic() - self.last_flush >= LOG_FLUSH_SECS):
                self._flush_locked()

    def flush(self):
        with self.lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self.buf:
            return
        rows = []
        for line in self.buf:
            self.seq += 1
            rows.append({"task_id": self.task_id, "seq": self.seq, "line": line})
        self.buf = []
        self.last_flush = time.monotonic()
        self.cloud.insert_logs(rows)


class Runner:
    """Holds the currently-running task so the command handler can stop it."""

    def __init__(self, cloud: Cloud, cfg):
        self.cloud = cloud
        self.cfg = cfg
        self.current_task_id: str | None = None
        self._stop_requested = threading.Event()

    def request_stop(self, task_id: str) -> bool:
        if self.current_task_id == task_id:
            self._stop_requested.set()
            return True
        return False

    # ── path resolution (port of resolve_path) ──────────────
    def resolve_path(self, task: dict) -> str:
        custom = task.get("scenario_path")
        if custom:
            return re.sub(r"(?i)\{\s*username\s*\}", self.cfg.username, custom)
        name = task["scenario_name"]
        if os.path.isabs(name):
            return name
        if not name.lower().endswith(".rks"):
            name += ".rks"
        return os.path.join(self.cfg.resolved_scenarios_folder(), name)

    # ── main entry ───────────────────────────────────────────
    def run(self, task: dict):
        task_id = task["id"]
        self.current_task_id = task_id
        self._stop_requested.clear()
        logs = LogBatcher(self.cloud, task_id)
        try:
            self._run_inner(task, task_id, logs)
        finally:
            logs.flush()
            self.current_task_id = None
            # Our run is over — reset the external-run monitor so residual
            # Keyence activity doesn't spawn a phantom on-PC task row
            try:
                from . import heartbeat
                heartbeat.monitor.force_end()
            except Exception:
                pass

    def _fail(self, task_id: str, logs: LogBatcher, msg: str):
        log.error(msg)
        logs.add(f"❌ {msg}")
        self.cloud.update_task(task_id, {
            "status": "failed", "error": msg, "finished_at": utcnow(),
        })

    def _run_inner(self, task: dict, task_id: str, logs: LogBatcher):
        rks = self.resolve_path(task)

        if not os.path.isfile(rks):
            return self._fail(task_id, logs, f"Scenario not found: {rks}")
        if not os.path.isfile(self.cfg.rk_exe):
            return self._fail(task_id, logs, f"RK-Keyence executable not found: {self.cfg.rk_exe}")

        self.cloud.update_task(task_id, {"resolved_path": rks})
        logs.add(f"▶ Starting on {self.cfg.username}")
        logs.add(f"   Path: {rks}")

        from . import rkdetect
        log_base = rkdetect.log_baseline()
        run_started_ts = time.time()

        try:
            proc = subprocess.Popen(
                [self.cfg.rk_exe, "/RUN", rks],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            return self._fail(task_id, logs, f"Failed to start process: {e}")

        stopped = threading.Event()

        def watch_stop():
            # Local stop signal is set by the command handler thread when a
            # stop_task command arrives; also honour a dashboard direct
            # status change to 'stopped' on a pending→running race.
            while not stopped.is_set():
                if self._stop_requested.wait(timeout=1):
                    log.info("Stop requested for task %s", task_id)
                    is_real_rk = os.path.basename(self.cfg.rk_exe).lower() == "rkscenariomanager.exe"
                    if is_real_rk:
                        send_ctrl_alt_p()
                        time.sleep(1.5)
                    if proc.poll() is None:
                        # Kill the whole tree so children release the stdout pipe
                        if platform.system() == "Windows":
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                capture_output=True, creationflags=CREATE_NO_WINDOW,
                            )
                        else:
                            proc.terminate()
                    # Belt and braces: kill the engine if it's still alive
                    if is_real_rk:
                        time.sleep(1)
                        if rk_engine_running():
                            subprocess.run(
                                ["taskkill", "/F", "/IM", "RkScenarioManager.exe"],
                                capture_output=True, creationflags=CREATE_NO_WINDOW,
                            )
                    logs.add("⛔ Stopped by user (CTRL+ALT+P sent)")
                    self.cloud.update_task(task_id, {
                        "status": "stopped", "finished_at": utcnow(),
                    })
                    stopped.set()
                    return

        stop_thread = threading.Thread(target=watch_stop, daemon=True)
        stop_thread.start()

        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    logs.add(line)
        except Exception:
            pass

        proc.wait()

        # Launcher exits early — wait until the run itself finishes: a fresh
        # RunningLog write or the runner processes going quiet. (The old
        # "wait until RkScenarioManager.exe disappears" never ended when the
        # Keyence app was open on the PC.) Skipped for stub exes in tests.
        if os.path.basename(self.cfg.rk_exe).lower() == "rkscenariomanager.exe":
            rkdetect.wait_run_end(self._stop_requested, log_base)

        stopped.set()

        if self._stop_requested.is_set():
            stop_thread.join(timeout=15)  # let watch_stop finish writing 'stopped'
            return

        # Surface Keyence's own error dumps in the task logs (informational —
        # the launcher exit code still decides the status)
        try:
            for m in rkdetect.new_error_logs(run_started_ts - 5)[:20]:
                logs.add(f"[Keyence error] {m}")
        except Exception:
            pass

        exit_code = proc.returncode
        ok = exit_code == 0
        logs.add("✅ Scenario completed" if ok else f"❌ Scenario failed (exit {exit_code})")
        err = None if ok else f"RkScenarioManager exited with code {exit_code}"
        if not ok:
            # A failed license verification blocks all runs — name the real cause
            try:
                from . import license
                lic = license.health(force=True)
                if lic["status"] in ("error", "warning") and lic.get("error"):
                    logs.add(f"[Keyence license] {lic['error']}")
                    if lic["status"] == "error":
                        err = f"License verification failed: {lic['error']}"
            except Exception:
                pass
        self.cloud.update_task(task_id, {
            "status": "success" if ok else "failed",
            "exit_code": exit_code,
            "error": err,
            "finished_at": utcnow(),
        })
        log.info("Task %s → %s (exit %s)", task_id, "success" if ok else "failed", exit_code)
