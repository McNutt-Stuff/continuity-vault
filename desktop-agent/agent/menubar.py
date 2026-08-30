"""
Friendly macOS menu-bar UI for the desktop agent.

Runs the agent loop in a background thread and shows a menu-bar item with status,
"Sync now", and log access. If `rumps` is unavailable, it falls back to the
headless loop so the agent still runs in the background.
"""

from __future__ import annotations

import subprocess
import threading
import time

from .config import Config
from .main import Agent
from . import native_status


def _agent_loop(agent: Agent, on_status) -> None:
    cfg = agent.cfg
    if not agent.registered and cfg.linking_code:
        try:
            agent.activate(cfg.linking_code)
        except Exception as exc:
            on_status(f"Activation failed: {exc}")
    while True:
        interval = (agent.reg or {}).get("heartbeat_interval_seconds", 30)
        try:
            if agent.registered:
                agent.heartbeat()  # pulls mappings + runs due collects (push model)
                on_status(f"Connected · {agent.reg.get('hostname', '')}")
            else:
                on_status("Not linked — enter a code")
        except Exception as exc:
            on_status(f"Error: {exc}")
        time.sleep(interval)


def run() -> None:
    cfg = Config()
    agent = Agent(cfg)

    # The menu-bar icon is opt-out via the cloud-driven agent config (Agents →
    # Configuration); a local ``no_tray`` marker wins for offline control. When
    # hidden — or when rumps isn't available — the agent runs fully headless. The
    # heartbeat restarts the process (launchd KeepAlive) if the preference flips.
    try:
        import rumps
        agent._tray_available = True
    except Exception:
        agent._tray_available = False
        agent._tray_mode = False
        agent.run()  # headless fallback (no menu-bar support)
        return

    show_tray = bool((agent.reg or {}).get("config", {}).get("show_tray_icon", True))
    if (cfg.data_dir / "no_tray").exists():
        show_tray = False
    if not show_tray:
        agent._tray_mode = False
        agent.run()  # headless — no menu-bar icon
        return
    agent._tray_mode = True

    class App(rumps.App):
        def __init__(self):
            super().__init__("Arkive", quit_button=None)
            self.status_item = rumps.MenuItem("Starting…")
            self.menu = [self.status_item, None,
                         rumps.MenuItem("Agent Status…", callback=self.show_status),
                         rumps.MenuItem("Sync now", callback=self.sync),
                         rumps.MenuItem("Open logs", callback=self.logs), None,
                         rumps.MenuItem("Quit", callback=self.quit)]
            threading.Thread(target=_agent_loop, args=(agent, self._set_status),
                             daemon=True).start()

        def _set_status(self, text: str):
            self.status_item.title = text

        def show_status(self, _):
            try:
                action = native_status.show(agent.status_snapshot())
                if action == "sync":
                    self.sync(None)
                elif action == "logs":
                    self.logs(None)
            except Exception as exc:
                rumps.notification("Arkive", "Could not open status", str(exc))

        def sync(self, _):
            try:
                agent.sync_now()
                rumps.notification("Arkive", "Sync started", "Collecting in the background…")
            except Exception as exc:
                rumps.notification("Arkive", "Sync failed", str(exc))

        def logs(self, _):
            subprocess.Popen(["open", str(cfg.data_dir / "agent.log")])

        def quit(self, _):
            rumps.quit_application()

    App().run()


if __name__ == "__main__":
    run()
