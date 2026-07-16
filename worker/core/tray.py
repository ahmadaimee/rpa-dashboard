"""System tray icon for the background worker.

Shows the RPA-Bot logo in the notification area with:
  - status line (PC + version)
  - Open Dashboard
  - Exit (marks the worker offline and stops the process)
"""
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from . import config as cfgmod

log = logging.getLogger("worker")


def _icon_image():
    from PIL import Image, ImageDraw
    # Bundled asset (PyInstaller --add-data) or repo assets/
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
    png = base / "assets" / "icon.png"
    if png.exists():
        return Image.open(png)
    # Fallback: plain blue square with a white dot
    img = Image.new("RGBA", (64, 64), (0, 120, 212, 255))
    ImageDraw.Draw(img).ellipse([20, 20, 44, 44], fill=(255, 255, 255, 255))
    return img


def start(cloud, cfg, version: str, dashboard_url: str, runner=None):
    """Launch the tray icon on a daemon thread. No-op if pystray is missing."""
    try:
        import pystray
        from pystray import Menu, MenuItem
    except Exception as e:
        log.warning("Tray unavailable (%s) — continuing without it", e)
        return None

    def open_dashboard(icon, item):
        webbrowser.open(dashboard_url)

    def check_updates(icon, item):
        from . import updater
        if runner and runner.current_task_id:
            icon.notify("A scenario is running — the update will apply once idle.", "RPA-Bot")
            return
        try:
            icon.notify("Checking for updates…", "RPA-Bot")
        except Exception:
            pass
        try:
            # check_and_apply exits the process if a newer version is found;
            # if we get here there was nothing to update.
            updater.check_and_apply(cloud, version)
            icon.notify(f"You are on the latest version (v{version}).", "RPA-Bot")
        except Exception as e:
            icon.notify(f"Update check failed: {e}", "RPA-Bot")

    def do_exit(icon, item):
        log.info("Exit chosen from tray — shutting down")
        try:
            cloud.set_status("offline")
        except Exception:
            pass
        icon.stop()
        os._exit(0)

    def status_text(item):
        running = runner.current_task_id if runner else None
        return (f"RPA-Bot v{version} — {cfg.username}"
                + (" (running)" if running else " (idle)"))

    icon = pystray.Icon(
        "RPA-Bot",
        _icon_image(),
        f"RPA-Bot — {cfg.username}",
        menu=Menu(
            # Invisible default → clicking the tray icon opens the dashboard
            MenuItem("Open Dashboard", open_dashboard, default=True, visible=False),
            MenuItem(status_text, None, enabled=False),
            MenuItem("Check for updates", check_updates),
            Menu.SEPARATOR,
            MenuItem("Exit", do_exit),
        ),
    )
    t = threading.Thread(target=icon.run, daemon=True, name="tray")
    t.start()
    log.info("Tray icon started")
    return icon
