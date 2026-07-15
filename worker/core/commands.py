"""Command polling + dispatch (stop_task / scan / shutdown / restart).

A background thread polls the commands table every POLL_SECS so stop_task
lands even while a scenario is running in the main loop.
"""
import logging
import os
import subprocess
import sys
import threading
import time

from .cloud import Cloud, utcnow
from .runner import Runner, send_ctrl_alt_p
from . import heartbeat, rkdetect, scanner, winsched

log = logging.getLogger("worker")

POLL_SECS = 5


class CommandHandler:
    def __init__(self, cloud: Cloud, cfg, runner: Runner):
        self.cloud = cloud
        self.cfg = cfg
        self.runner = runner
        self.shutdown = threading.Event()   # main loop watches this
        self.update_requested = False       # main loop applies when idle

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, daemon=True, name="commands")
        t.start()
        return t

    def _loop(self):
        while not self.shutdown.is_set():
            try:
                for cmd in self.cloud.pending_commands():
                    self._handle(cmd)
            except Exception as e:
                log.debug("Command loop error: %s", e)
            time.sleep(POLL_SECS)

    def _stop_external(self, task_id: str) -> bool:
        """Stop a scenario the user started directly on this PC."""
        task = self.cloud.get_task(task_id)
        if not task or task.get("source") != "external" or task.get("status") != "running":
            return False
        log.info("Stopping external Keyence run (task %s)", task_id)
        send_ctrl_alt_p()               # Keyence's graceful stop hotkey
        time.sleep(2)
        rkdetect.kill_keyence_dotnets(heartbeat.monitor._active_pids or None)
        self.cloud.update_task(task_id, {
            "status": "stopped",
            "error": "Stopped from dashboard",
            "finished_at": utcnow(),
        })
        heartbeat.set_external_stopped()
        return True

    def _handle(self, cmd: dict):
        ctype = cmd.get("type")
        payload = cmd.get("payload") or {}
        log.info("Command received: %s %s", ctype, payload)
        self.cloud.ack_command(cmd["id"])

        try:
            if ctype == "stop_task":
                task_id = payload.get("task_id")
                if task_id and self.runner.request_stop(task_id):
                    self.cloud.finish_command(cmd["id"], True, {"note": "stop signalled"})
                elif task_id and self._stop_external(task_id):
                    self.cloud.finish_command(cmd["id"], True, {"note": "external run stopped"})
                else:
                    # Not running here (already finished, or still pending —
                    # pending tasks are stopped directly by the dashboard).
                    self.cloud.finish_command(cmd["id"], True,
                                              {"note": "task not running on this worker"})

            elif ctype == "win_sched_list":
                result = winsched.sync(self.cloud)
                self.cloud.finish_command(cmd["id"], True, result)

            elif ctype == "win_sched_create":
                task_name = winsched.create_task(
                    payload["name"], payload["scenario"],
                    payload.get("days") or [], payload.get("time") or "00:00",
                )
                winsched.sync(self.cloud)
                self.cloud.finish_command(cmd["id"], True, {"task_name": task_name})

            elif ctype == "win_sched_delete":
                winsched.delete_task(payload["task_name"])
                winsched.sync(self.cloud)
                self.cloud.finish_command(cmd["id"], True)

            elif ctype == "win_sched_update":
                task_name = winsched.update_task(
                    payload["task_name"], payload["name"], payload["scenario"],
                    payload.get("days") or [], payload.get("time") or "00:00",
                )
                winsched.sync(self.cloud)
                self.cloud.finish_command(cmd["id"], True, {"task_name": task_name})

            elif ctype == "win_sched_toggle":
                winsched.toggle_task(payload["task_name"], bool(payload.get("enable")))
                winsched.sync(self.cloud)
                self.cloud.finish_command(cmd["id"], True)

            elif ctype == "scan":
                result = scanner.scan(self.cloud, self.cfg,
                                      folder=payload.get("folder") or None)
                self.cloud.finish_command(cmd["id"], result["error"] is None, result)

            elif ctype == "shutdown":
                self.cloud.finish_command(cmd["id"], True)
                self.cloud.set_status("offline")
                log.info("Shutdown requested by dashboard — exiting")
                self.shutdown.set()
                from .config import PID_FILE
                PID_FILE.unlink(missing_ok=True)  # os._exit skips atexit
                os._exit(0)

            elif ctype == "restart":
                self.cloud.finish_command(cmd["id"], True)
                self.cloud.set_status("offline")
                log.info("Restart requested by dashboard — relaunching")
                exe = sys.executable
                args = [exe] if getattr(sys, "frozen", False) else [exe, sys.argv[0]]
                # NOTE: never combine DETACHED_PROCESS with CREATE_NO_WINDOW —
                # the child silently fails to run.
                subprocess.Popen(args + ["--background"], creationflags=(
                    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP))
                from .config import PID_FILE
                PID_FILE.unlink(missing_ok=True)  # os._exit skips atexit
                os._exit(0)

            elif ctype == "update":
                self.update_requested = True
                self.cloud.finish_command(cmd["id"], True,
                                          {"note": "update check scheduled (applies when idle)"})

            else:
                self.cloud.finish_command(cmd["id"], False,
                                          {"error": f"Unknown command type: {ctype}"})
        except Exception as e:
            log.exception("Command %s failed", ctype)
            self.cloud.finish_command(cmd["id"], False, {"error": str(e)})
