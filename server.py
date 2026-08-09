"""server.py — PylonRack slot: services.

Runs several llama-server instances side by side and turns the Mac into an
inference node the home lab can reach.

Starting at login is not handled here: PylonRack already does it, and two
switches for one behaviour can only disagree.

Two channels:
  * ws://127.0.0.1:8766  — the rack protocol (header controls, status, log)
  * http://127.0.0.1:8768 — the slot's own body panel, shown in the WebView

Header controls (declared here, rendered natively by the rack):
  start_all / stop_all  buttons
  status_label          label
  node_label            label — where the home lab should connect

The body is a single panel with its own two views, Instances and Log. The
rack's generic log panel shows one undivided stream; with several servers
running, the only useful log is one you can filter, so it lives here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import websockets

import config as cfg_module
from instances import InstanceManager, RUNNING, STARTING, MISSING
from model_scanner import scan
from parent_watchdog import watch_parent
from uiserver import UIServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("services")

SNAPSHOT_INTERVAL = 2.0     # how often the page refreshes itself


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self) -> None:
        self.cfg = cfg_module.load()
        self.mgr = InstanceManager(self.cfg)
        self.log_subscribers: set = set()
        self.busy: str = ""        # non-empty while a blocking op is running

    def save(self) -> None:
        self.mgr.remember_running()
        cfg_module.save(self.cfg)


# ---------------------------------------------------------------------------
# Rack protocol
# ---------------------------------------------------------------------------

def _status_text(st: AppState) -> str:
    if st.busy:
        return st.busy
    snap = st.mgr.snapshot()
    if snap["running"] == 0:
        return "Idle"
    return f"{snap['running']}/{snap['total']} running · {snap['used_gb']} GB"


def _status_style(st: AppState) -> str:
    if st.busy:
        return "warning"
    return "success" if st.mgr.snapshot()["running"] else "default"


def _controls(st: AppState) -> list[dict]:
    snap = st.mgr.snapshot()
    any_stopped = any(r["state"] not in (RUNNING, STARTING, MISSING)
                      for r in snap["instances"])
    return [
        {"id": "start_all", "type": "button", "label": "Start all",
         "style": "primary" if any_stopped else "default",
         "icon": "play.fill", "position": "leading",
         "tooltip": "Start every stopped instance, largest model first"},
        {"id": "stop_all", "type": "button", "label": "Stop all",
         "style": "default", "icon": "stop.fill", "position": "leading",
         "tooltip": "Stop every running instance"},
        {"id": "status_label", "type": "label", "value": _status_text(st),
         "style": _status_style(st), "position": "leading"},
        {"id": "node_label", "type": "label",
         "value": f"{snap['lan_ip']}", "style": "default", "position": "trailing",
         "tooltip": "Address the home lab should use to reach this node"},
    ]


def _manifest(st: AppState) -> dict:
    return {
        "type": "manifest",
        "name": "services",
        "version": "1.0",
        "heartbeat_interval": 5,
        "modes": ["instances"],
        "ui_url": f"http://127.0.0.1:{st.cfg.ui_port}/",
        "controls": _controls(st),
    }


def _controls_update(st: AppState) -> dict:
    return {"type": "controls_update", "controls": _controls(st)}


def _pong(st: AppState) -> dict:
    snap = st.mgr.snapshot()
    running = snap["running"] > 0
    if st.busy:
        msg, status = st.busy, "warning"
    elif running:
        reqs = sum(r["requests"] for r in snap["instances"])
        msg = f"{snap['used_gb']} GB · {reqs} req · {snap['running']}/{snap['total']}"
        status = "running"
    else:
        msg, status = "Idle", "warning"
    return {"type": "pong", "status": status, "message": msg}


# ---------------------------------------------------------------------------
# Commands — shared by the rack header and the body page
# ---------------------------------------------------------------------------

class Commands:
    """One implementation, two callers. The header and the page must never
    disagree about what 'start' means."""

    def __init__(self, st: AppState) -> None:
        self.st = st
        self.ui: UIServer | None = None

    async def _blocking(self, label: str, fn):
        """Run a slow call off the loop while the UI keeps updating."""
        self.st.busy = label
        await self.broadcast()
        try:
            return await asyncio.get_event_loop().run_in_executor(None, fn)
        finally:
            self.st.busy = ""
            self.st.save()
            await self.broadcast()

    async def broadcast(self) -> None:
        if self.ui:
            await self.ui.push("snapshot", self.snapshot())
        await _rack_broadcast(self.st)

    def snapshot(self) -> dict:
        snap = self.st.mgr.snapshot()
        snap["busy"] = self.st.busy
        snap["llama_bin"] = str(self.st.cfg.bin_path)
        snap["llama_bin_ok"] = self.st.cfg.bin_path.exists()
        snap["hf_cache"] = str(self.st.cfg.hf_cache_path)
        return snap

    # ── actions ───────────────────────────────────────────────────────
    async def run(self, action: str, p: dict) -> dict:
        st = self.st

        if action == "snapshot":
            return {"ok": True, "snapshot": self.snapshot()}

        if action == "start":
            ok, msg = await self._blocking(
                "Starting…", lambda: st.mgr.start(p["id"]))
            return {"ok": ok, "message": msg}

        if action == "stop":
            await self._blocking("Stopping…", lambda: st.mgr.stop(p["id"]))
            return {"ok": True}

        if action == "start_all":
            res = await self._blocking("Starting…", st.mgr.start_all)
            failed = [(i, m) for i, ok, m in res if not ok]
            return {"ok": not failed,
                    "message": "; ".join(f"{i}: {m}" for i, m in failed)}

        if action == "stop_all":
            await self._blocking("Stopping…", st.mgr.stop_all)
            return {"ok": True}

        if action == "available_models":
            found = await asyncio.get_event_loop().run_in_executor(
                None, lambda: scan(st.cfg.hf_cache_path))
            used = {i.model_path for i in st.cfg.instances}
            return {"ok": True, "models": [
                {"name": m.display_name, "path": m.full_path,
                 "size_gb": m.size_gb, "added": m.full_path in used}
                for m in found]}

        if action == "add":
            return await self._add(p)

        if action == "update":
            return await self._update(p)

        if action == "remove":
            inst = st.cfg.find(p["id"])
            if not inst:
                return {"ok": False, "error": "no such instance"}
            await self._blocking("Stopping…", lambda: st.mgr.stop(p["id"]))
            st.cfg.instances = [i for i in st.cfg.instances if i.id != p["id"]]
            st.mgr.sync()
            st.save()
            await self.broadcast()
            return {"ok": True}

        if action == "relocate":
            inst = st.cfg.find(p["id"])
            path = (p.get("path") or "").strip()
            if not inst:
                return {"ok": False, "error": "no such instance"}
            if not Path(path).is_file():
                return {"ok": False, "error": f"not a file: {path}"}
            inst.model_path = path
            inst.size_gb = round(Path(path).stat().st_size / (1024 ** 3), 1)
            st.mgr.sync()
            st.save()
            await self.broadcast()
            return {"ok": True}

        if action == "log":
            m = st.mgr.get(p.get("id", ""))
            if m is None:
                merged: list[str] = []
                for mi in st.mgr.all():
                    merged += [f"{mi.inst.name[:14]:<14} {ln}"
                               for ln in mi.log_tail(80)]
                return {"ok": True, "lines": merged[-400:]}
            return {"ok": True, "lines": m.log_tail(int(p.get("lines", 300)))}

        return {"ok": False, "error": f"unknown action {action}"}

    # ── add / edit ────────────────────────────────────────────────────
    async def _add(self, p: dict) -> dict:
        st = self.st
        path = (p.get("path") or "").strip()
        if not Path(path).is_file():
            return {"ok": False, "error": f"not a file: {path}"}
        if any(i.model_path == path for i in st.cfg.instances):
            return {"ok": False, "error": "this model already has an instance"}
        try:
            port = st.cfg.next_free_port()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}

        name = p.get("name") or Path(path).stem
        inst = cfg_module.Instance(
            id=uuid.uuid4().hex[:8],
            name=name[:48],
            model_path=path,
            port=port,
            size_gb=round(Path(path).stat().st_size / (1024 ** 3), 1),
        )
        st.cfg.instances.append(inst)
        st.mgr.sync()
        st.save()
        await self.broadcast()
        return {"ok": True, "id": inst.id}

    async def _update(self, p: dict) -> dict:
        st = self.st
        inst = st.cfg.find(p.get("id", ""))
        if not inst:
            return {"ok": False, "error": "no such instance"}

        fields = p.get("fields", {})
        ints = ("port", "ctx_size", "parallel", "threads",
                "n_gpu_layers", "batch_size", "ubatch_size", "top_k")
        floats = ("temperature", "top_p", "repeat_penalty")
        bools = ("flash_attn", "mlock")

        try:
            for k, v in fields.items():
                if not hasattr(inst, k):
                    continue
                if k in ints:
                    v = int(v)
                elif k in floats:
                    v = float(v)
                elif k in bools:
                    v = bool(v)
                else:
                    v = str(v)
                setattr(inst, k, v)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"bad value: {exc}"}

        if st.cfg.port_conflict(inst.port, inst.id):
            return {"ok": False,
                    "error": f"port {inst.port} is used by another instance"}
        if inst.ctx_size < 512:
            return {"ok": False, "error": "context must be at least 512"}
        if inst.parallel < 1:
            return {"ok": False, "error": "parallel must be at least 1"}

        st.mgr.sync()
        st.save()
        await self.broadcast()
        m = st.mgr.get(inst.id)
        note = ("restart the instance for the changes to take effect"
                if m and m.state == RUNNING else "")
        return {"ok": True, "message": note}


# ---------------------------------------------------------------------------
# Rack WebSocket
# ---------------------------------------------------------------------------

class SlotHandler:
    def __init__(self, st: AppState, cmd: Commands) -> None:
        self.st = st
        self.cmd = cmd
        self.clients: set = set()

    async def handle(self, ws) -> None:
        log.info("rack connected from %s", ws.remote_address)
        self.clients.add(ws)
        try:
            async for raw in ws:
                await self._dispatch(ws, raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            self.st.log_subscribers.discard(ws)
        log.info("rack disconnected")

    async def _dispatch(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = msg.get("type", "")

        if t == "manifest":
            await ws.send(json.dumps(_manifest(self.st)))
            await ws.send(json.dumps(_controls_update(self.st)))

        elif t == "ping":
            await ws.send(json.dumps(_pong(self.st)))

        elif t == "action":
            cid = msg.get("control_id", "")
            if cid in ("start_all", "stop_all"):
                await self.cmd.run(cid, {})

        elif t == "log_request":
            self.st.log_subscribers.add(ws)
            res = await self.cmd.run("log", {})
            await ws.send(json.dumps({
                "type": "log_response",
                "lines": res.get("lines", []),
                "total": len(res.get("lines", [])),
            }))

        elif t == "shutdown":
            log.info("shutdown requested by rack")
            self.st.save()
            self.st.mgr.stop_all()


_handler: SlotHandler | None = None


async def _rack_broadcast(st: AppState) -> None:
    if _handler is None:
        return
    payload = json.dumps(_controls_update(st))
    for ws in list(_handler.clients):
        try:
            await ws.send(payload)
        except Exception:
            _handler.clients.discard(ws)


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------

async def _tick(cmd: Commands) -> None:
    """Keep the page and the header honest about reality — a process can die
    on its own, and a model file can disappear while nobody is looking."""
    last = None
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL)
        try:
            snap = cmd.snapshot()
            if cmd.ui:
                await cmd.ui.push("snapshot", snap)
            fingerprint = [(r["id"], r["state"]) for r in snap["instances"]]
            if fingerprint != last:
                last = fingerprint
                await _rack_broadcast(cmd.st)
        except Exception:
            log.exception("tick failed")


def _task_finished(task: "asyncio.Task") -> None:
    """A background task must never end quietly. If one dies the slot keeps
    running with a piece missing, which is the hardest kind of fault to see."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task %s died: %r", task.get_name(), exc)
    else:
        log.warning("background task %s ended unexpectedly", task.get_name())


async def _shutdown(st: AppState) -> None:
    st.save()
    await asyncio.get_event_loop().run_in_executor(None, st.mgr.stop_all)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    global _handler

    rack_json = Path(__file__).parent / "rack.json"
    manifest = json.loads(rack_json.read_text(encoding="utf-8"))
    port = int(os.environ.get("PYLON_PORT", manifest.get("port", 8766)))

    st = AppState()
    cmd = Commands(st)
    _handler = SlotHandler(st, cmd)

    ui = UIServer(st.cfg.ui_port, cmd.run)
    cmd.ui = ui

    # Keep strong references. asyncio only holds a weak reference to a task,
    # so a create_task() whose return value is discarded can be garbage
    # collected mid-flight — here the UI server vanished after serving its
    # first page, and the slot went on running without a body panel.
    background = {
        asyncio.create_task(watch_parent(), name="watchdog"),
        asyncio.create_task(ui.serve(), name="ui"),
        asyncio.create_task(_tick(cmd), name="tick"),
    }
    for task in background:
        task.add_done_callback(_task_finished)

    # Bring back whatever was running before the last shutdown.
    restored = await asyncio.get_event_loop().run_in_executor(None, st.mgr.restore)
    for iid, ok, msg in restored:
        log.info("restore %s: %s %s", iid, "ok" if ok else "FAILED", msg)

    log.info("services slot on ws://127.0.0.1:%d, ui on :%d",
             port, st.cfg.ui_port)
    try:
        async with websockets.serve(_handler.handle, "127.0.0.1", port):
            await asyncio.Future()
    finally:
        await _shutdown(st)


if __name__ == "__main__":
    import traceback
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except BaseException:
        # A slot that dies without saying why is a slot nobody can fix.
        log.error("slot exited with an exception:\n%s", traceback.format_exc())
        raise
    else:
        log.warning("main() returned — the slot is shutting down")
