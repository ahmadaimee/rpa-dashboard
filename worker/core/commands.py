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

from .cloud import Cloud
from .runner import Runner
from . import scanner, winsched

log = logging.getLogger("worker")

POLL_SECS = 5


class CommandHandler:
    def __init__(self, cloud: Cloud, cfg, runner: Runner):
        self.cloud = cloud
        self.cfg = cfg
        self.runner = runner
        self.shutdown = threading.Event()   # main loop watches this
        self.restart = False

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

            elif ctype == "scan":
                result = scanner.scan(self.cloud, self.cfg,
                                      folder=payload.get("folder") or None)
                self.cloud.finish_command(cmd["id"], result["error"] is None, result)

            elif ctype == "shutdown":
                self.cloud.finish_command(cmd["id"], True)
                self.cloud.set_status("offline")
                log.info("Shutdown requested by dashboard — exiting")
                self.shutdown.set()
                os._exit(0)

            elif ctype == "restart":
                self.cloud.finish_command(cmd["id"], True)
                self.cloud.set_status("offline")
                log.info("Restart requested by dashboard — relaunching")
                exe = sys.executable
                args = [exe] if getattr(sys, "frozen", False) else [exe, sys.argv[0]]
                subprocess.Popen(args + ["--background"], creationflags=(
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW))
                os._exit(0)

            else:
                self.cloud.finish_command(cmd["id"], False,
                                          {"error": f"Unknown command type: {ctype}"})
        except Exception as e:
            log.exception("Command %s failed", ctype)
            self.cloud.finish_command(cmd["id"], False, {"error": str(e)})
