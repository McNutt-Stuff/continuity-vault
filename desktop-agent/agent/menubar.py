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
                agent.heartbeat()
                sched = agent.reg.get("config", {}).get("schedule_minutes", 360)
                if time.time() - agent._last_collect >= sched * 60:
                    agent.collect_and_push()
                on_status(f"Connected · {agent.reg.get('hostname', '')}")
            else:
                on_status("Not linked — enter a code")
        except Exception as exc:
            on_status(f"Error: {exc}")
        time.sleep(interval)


def run() -> None:
    cfg = Config()
    agent = Agent(cfg)

    try:
        import rumps
    except Exception:
        agent.run()  # headless fallback
        return

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
                r = agent.collect_and_push()
                rumps.notification("Arkive", "Sync complete", f"{r.get('objects', 0)} items")
            except Exception as exc:
                rumps.notification("Arkive", "Sync failed", str(exc))

        def logs(self, _):
            subprocess.Popen(["open", str(cfg.data_dir / "agent.log")])

        def quit(self, _):
            rumps.quit_application()

    App().run()


if __name__ == "__main__":
    run()
