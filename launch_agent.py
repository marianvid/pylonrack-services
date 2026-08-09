"""launch_agent.py — start PylonRack at login.

macOS runs per-user background items from ~/Library/LaunchAgents. The toggle in
the slot writes (or removes) one plist there and loads it, so the inference
node is up before anyone opens anything.

The slot itself is started by PylonRack, so there is nothing separate to
register for it — "load the services slot automatically" is a flag the rack
reads, not another launch agent.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.marianvid.pylonrack.login"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
APP_CANDIDATES = [
    Path("/Applications/PylonRack.app"),
    Path.home() / "Applications" / "PylonRack.app",
]


def app_path() -> Path | None:
    for p in APP_CANDIDATES:
        if p.exists():
            return p
    return None


def is_enabled() -> bool:
    return PLIST.exists()


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """Create or remove the login item. Returns (ok, error)."""
    try:
        if enabled:
            app = app_path()
            if app is None:
                return False, ("PylonRack.app not found in /Applications "
                               "or ~/Applications")
            PLIST.parent.mkdir(parents=True, exist_ok=True)
            plist = {
                "Label": LABEL,
                "ProgramArguments": ["/usr/bin/open", "-a", str(app)],
                "RunAtLoad": True,
                # Deliberately no KeepAlive: this starts the app once at login.
                # Respawning a GUI app the user just quit would be hostile.
                "ProcessType": "Interactive",
            }
            with open(PLIST, "wb") as fh:
                plistlib.dump(plist, fh)
            _bootstrap(load=True)
        else:
            _bootstrap(load=False)
            if PLIST.exists():
                PLIST.unlink()
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _bootstrap(load: bool) -> None:
    """launchctl bootstrap/bootout, tolerating an already-(un)loaded agent."""
    uid = os.getuid()
    domain = f"gui/{uid}"
    if load:
        cmd = ["launchctl", "bootstrap", domain, str(PLIST)]
    else:
        cmd = ["launchctl", "bootout", f"{domain}/{LABEL}"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=15)
