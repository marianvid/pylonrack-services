"""uiserver.py — HTTP + WebSocket server for the slot's own body panel.

PylonRack renders a slot body either as one of its native Swift views or as a
WebView pointed at `ui_url`. A table of N instances is not one of the native
views, and adding one would mean rebuilding the rack app — so the slot serves
its own page and the rack just displays it.

One port carries both: `websockets.serve` hands plain HTTP requests to
`process_request`, and anything asking for an upgrade becomes a live channel.
The page pulls a snapshot on connect and then receives pushes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from http import HTTPStatus
from pathlib import Path

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent / "ui"


class UIServer:
    """Serves ui/ over HTTP and keeps the page in sync over a WebSocket."""

    def __init__(self, port: int, on_command) -> None:
        self.port = port
        self._on_command = on_command      # async (action, payload) -> dict
        self._clients: set = set()

    # ── HTTP ──────────────────────────────────────────────────────────
    @staticmethod
    def _reply(status: HTTPStatus, body: bytes, ctype: str) -> Response:
        """Build the response by hand.

        `connection.respond()` already fills in Content-Type and
        Content-Length; setting them again appends duplicates rather than
        replacing, and the client then hangs on a malformed response. That
        cost an hour, so it is written down.
        """
        return Response(
            int(status), status.phrase,
            Headers({
                "Content-Type": ctype,
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
                "Connection": "close",
            }),
            body,
        )

    async def _process_request(self, connection, request):
        """Return an HTTP response for plain requests, None to let it upgrade."""
        path = request.path.split("?")[0]
        if path == "/ws":
            return None

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        try:
            target = (UI_DIR / rel).resolve()
            target.relative_to(UI_DIR.resolve())     # no escaping the ui dir
        except ValueError:
            return self._reply(HTTPStatus.FORBIDDEN, b"forbidden\n", "text/plain")
        if not target.is_file():
            return self._reply(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        return self._reply(HTTPStatus.OK, target.read_bytes(), ctype)

    # ── WebSocket ─────────────────────────────────────────────────────
    async def _handle(self, ws) -> None:
        self._clients.add(ws)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                action = msg.get("action", "")
                payload = msg.get("payload", {})
                try:
                    result = await self._on_command(action, payload)
                except Exception as exc:          # never let the page hang
                    log.exception("ui command %s failed", action)
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                await ws.send(json.dumps({
                    "type": "result",
                    "action": action,
                    "req": msg.get("req"),
                    "data": result,
                }))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    async def push(self, kind: str, data) -> None:
        """Broadcast to every open page. A send that fails drops the client —
        swallowing the error is how a dead socket keeps receiving into a void."""
        if not self._clients:
            return
        payload = json.dumps({"type": kind, "data": data})
        for ws in list(self._clients):
            try:
                await ws.send(payload)
            except Exception:
                self._clients.discard(ws)

    async def serve(self) -> None:
        log.info("UI on http://127.0.0.1:%d", self.port)
        async with websockets.serve(
            self._handle, "127.0.0.1", self.port,
            process_request=self._process_request,
            ping_interval=20, ping_timeout=20,
        ):
            await asyncio.Future()
