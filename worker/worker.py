"""
Orchard RPA Worker — cloud edition
==================================

First run (double-click the exe): asks for a pairing code, registers with
Supabase, installs a Task Scheduler logon task, and starts hidden.

    OrchardRPAWorker.exe                first-run setup / "already running" notice
    OrchardRPAWorker.exe --background   the hidden worker loop (set by Task Scheduler)
    OrchardRPAWorker.exe --uninstall    remove autostart + stop the worker

Success/Failed: RkScenarioManager.exe /RUN exits 0 → success, non-zero →
failed. Full stdout is streamed to the cloud task_logs table.
"""
import argparse
import logging
import platform
import sys
import threading
import time

from version import __version__
import embedded
from core import config as cfgmod
from core import installer, scanner, heartbeat
from core.cloud import Cloud
from core.commands import CommandHandler
from core.runner import Runner

POLL_INTERVAL = 2  # s — task pickup cadence

log = logging.getLogger("worker")


def _setup_logging(to_file: bool):
    logging.basicConfig(
        level=logging.INFO,
        format=f"[%(asctime)s] [{cfgmod.USERNAME}] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if to_file:
        try:
            from logging.handlers import RotatingFileHandler
            cfgmod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(cfgmod.LOG_FILE, maxBytes=2_000_000,
                                     backupCount=2, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                f"[%(asctime)s] [{cfgmod.USERNAME}] %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            logging.getLogger().addHandler(fh)
        except Exception:
            pass


def _hide_console():
    """Hide our console window in --background mode (exe is a console build
    so the first-run pairing prompt works; background runs go invisible)."""
    if platform.system() == "Windows":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass


def _set_console_title():
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(f"OrchardRPA-Worker [{cfgmod.USERNAME}]")
        except Exception:
            pass


def parse_args():
    p = argparse.ArgumentParser(description="Orchard RPA Worker (cloud)")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--uninstall", action="store_true", help="Remove autostart and stop the worker")
    return p.parse_args()


def worker_loop(cfg: cfgmod.Config):
    cloud = Cloud(cfg)
    cloud.sign_in()
    runner = Runner(cloud, cfg)
    cmds = CommandHandler(cloud, cfg, runner)

    cloud.set_status("idle")
    scanner.scan(cloud, cfg)  # auto-scan on startup
    heartbeat.start(cloud, runner, __version__)
    cmds.start()

    log.info("Worker %s online (v%s)", cfg.worker_id, __version__)

    disabled_logged = False
    tick = 0
    while not cmds.shutdown.is_set():
        try:
            tick += 1
            # Respect the dashboard 'enabled' switch (checked every ~30 s)
            if tick % 15 == 1:
                if not cloud.worker_enabled():
                    if not disabled_logged:
                        log.warning("Worker disabled from dashboard — idling")
                        disabled_logged = True
                    time.sleep(POLL_INTERVAL)
                    continue
                disabled_logged = False

            task = cloud.claim_next_task()
            if task:
                log.info("Running task %s (%s)", task["id"], task["scenario_name"])
                runner.run(task)
        except Exception as e:
            log.exception("Loop error: %s", e)
        time.sleep(POLL_INTERVAL)


def main():
    args = parse_args()

    if args.uninstall:
        _setup_logging(to_file=False)
        cfg = cfgmod.load()
        cloud = None
        if cfg:
            try:
                cloud = Cloud(cfg)
                cloud.sign_in()
            except Exception:
                cloud = None
        installer.uninstall(cloud)
        input("  Press Enter to close...")
        return

    cfg = cfgmod.load()

    # ── Background mode (Task Scheduler / relaunch) ─────────
    if args.background:
        _hide_console()
        _setup_logging(to_file=True)
        if not cfg:
            log.error("No config found — run the installer first (double-click the exe)")
            return
        if installer.is_already_running():
            log.info("Another worker instance is already running — exiting")
            return
        installer.acquire_instance_lock()
        _set_console_title()
        worker_loop(cfg)
        return

    # ── Interactive (double-click) ──────────────────────────
    _setup_logging(to_file=False)
    if cfg and installer.is_already_running():
        print()
        print("  ✅ Orchard RPA Worker is already running in the background.")
        print()
        input("  Press Enter to close this window...")
        return

    if cfg:
        # Configured but not running — reinstall task and start
        print("  Existing registration found — starting worker...")
        installer.install_startup_task()
        installer.relaunch_background()
        time.sleep(3)
        print("  ✅ Worker started in the background.")
        time.sleep(2)
        return

    installer.interactive_install(embedded.SUPABASE_URL, embedded.SUPABASE_ANON_KEY, __version__)


if __name__ == "__main__":
    main()
