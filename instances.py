"""instances.py — run several llama-server processes side by side.

The llama slot manages exactly one process. This manages N, each with its own
port, model and parameters. Everything that differs from the single-process
case lives here:

  * a memory guard, checked BEFORE spawning — three big models that do not fit
    would otherwise wedge the machine
  * a `missing` state for a model file that vanished from disk, kept separate
    from `error`, because an absence is not a fault
  * per-instance log files with rotation, so one chatty model cannot bury the
    others
  * readiness polling against the instance's own port

Nothing here touches the event loop; the caller runs these in an executor.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import psutil
import requests

log = logging.getLogger(__name__)

LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 5
READY_TIMEOUT = 300          # a 20 GB model on a cold cache is genuinely slow
READY_POLL = 1.0
MEM_HEADROOM_GB = 8.0        # never plan to fill RAM to the brim

# Instance states. `missing` is deliberately not `error`.
IDLE = "idle"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
ERROR = "error"
MISSING = "missing"


class ManagedInstance:
    """Lifecycle of one llama-server process."""

    def __init__(self, inst, cfg) -> None:
        self.inst = inst
        self.cfg = cfg
        self._proc: subprocess.Popen | None = None
        self._state: str = IDLE
        self._detail: str = ""
        self._started_at: float = 0.0
        self._lock = threading.Lock()

    # ── state ─────────────────────────────────────────────────────────
    @property
    def state(self) -> str:
        """Current state, re-derived from reality rather than remembered.

        A remembered state drifts: the process can die without telling us and
        the row keeps claiming Running. The model file can be deleted while an
        instance is idle and nothing notices until a confusing start failure.
        """
        if self._state in (STARTING, STOPPING):
            return self._state
        if self._proc is not None and self._proc.poll() is None:
            return RUNNING
        if not Path(self.inst.model_path).exists():
            return MISSING
        if self._state == ERROR:
            return ERROR
        return IDLE

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc and self._proc.poll() is None else None

    @property
    def uptime(self) -> int:
        return int(time.time() - self._started_at) if self.pid else 0

    def rss_gb(self) -> float:
        pid = self.pid
        if not pid:
            return 0.0
        try:
            return round(psutil.Process(pid).memory_info().rss / (1024 ** 3), 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0

    def requests_active(self) -> int:
        if self.state != RUNNING:
            return 0
        try:
            r = requests.get(
                f"http://127.0.0.1:{self.inst.port}/metrics", timeout=1.5)
            r.raise_for_status()
            for line in r.text.splitlines():
                if line.startswith("llamacpp:requests_processing "):
                    return int(float(line.split()[1]))
        except Exception:
            pass
        return 0

    # ── lifecycle ─────────────────────────────────────────────────────
    def build_command(self) -> list[str]:
        i = self.inst
        cmd = [
            str(self.cfg.bin_path),
            "--host", i.host,
            "--port", str(i.port),
            "-m", i.model_path,
            "--ctx-size", str(i.ctx_size),
            "--parallel", str(i.parallel),
            "--threads", str(i.threads),
            "--n-gpu-layers", str(i.n_gpu_layers),
            "--batch-size", str(i.batch_size),
            "--ubatch-size", str(i.ubatch_size),
            "--temp", str(i.temperature),
            "--top-p", str(i.top_p),
            "--top-k", str(i.top_k),
            "--repeat-penalty", str(i.repeat_penalty),
        ]
        if i.flash_attn:
            cmd += ["--flash-attn", "on"]
        if i.mlock:
            cmd += ["--mlock"]
        if i.api_key:
            cmd += ["--api-key", i.api_key]
        if i.draft_model_path and Path(i.draft_model_path).exists():
            cmd += ["--model-draft", i.draft_model_path]
        return cmd

    def start(self) -> tuple[bool, str]:
        """Spawn and wait until the port answers. Returns (ok, message)."""
        with self._lock:
            if self.state == RUNNING:
                return True, "already running"
            if not Path(self.inst.model_path).exists():
                self._state = MISSING
                self._detail = "model file not found"
                return False, self._detail
            if not self.cfg.bin_path.exists():
                self._state = ERROR
                self._detail = f"llama-server not found at {self.cfg.bin_path}"
                return False, self._detail

            self._state = STARTING
            self._detail = ""

        log_path = self.cfg.log_file_for(self.inst.id)
        _rotate(log_path)
        try:
            fh = open(log_path, "a", buffering=1, encoding="utf-8",
                      errors="replace")
            fh.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"port {self.inst.port} ===\n")
            self._proc = subprocess.Popen(
                self.build_command(),
                stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True,   # survives a rack crash; watchdog cleans up
            )
        except Exception as exc:
            self._state = ERROR
            self._detail = str(exc)
            return False, self._detail

        self._started_at = time.time()
        ok = self._wait_ready()
        if ok:
            self._state = RUNNING
            self._detail = ""
            return True, "running"

        # Did not answer in time — do not leave a half-alive process behind.
        self._detail = _last_lines(log_path, 3) or "did not become ready"
        self.stop()
        self._state = ERROR
        return False, self._detail

    def _wait_ready(self) -> bool:
        url = f"http://127.0.0.1:{self.inst.port}/health"
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return False          # died during load
            try:
                if requests.get(url, timeout=2).status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(READY_POLL)
        return False

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                if self._state not in (ERROR, MISSING):
                    self._state = IDLE
                return
            self._state = STOPPING

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            log.warning("instance %s ignored SIGTERM, killing", self.inst.id)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

        self._proc = None
        self._state = IDLE
        self._detail = ""

    def log_tail(self, n: int = 200) -> list[str]:
        p = self.cfg.log_file_for(self.inst.id)
        if not p.exists():
            return []
        return p.read_text(errors="replace").splitlines()[-n:]


class InstanceManager:
    """Owns every ManagedInstance and the rules that span them."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._managed: dict = {}
        self.sync()

    def sync(self) -> None:
        """Reconcile the managed set with the config, keeping live processes."""
        for inst in self.cfg.instances:
            m = self._managed.get(inst.id)
            if m is None:
                self._managed[inst.id] = ManagedInstance(inst, self.cfg)
            else:
                m.inst = inst          # config edited; keep the running process
        for gone in set(self._managed) - {i.id for i in self.cfg.instances}:
            self._managed[gone].stop()
            del self._managed[gone]

    def get(self, inst_id: str):
        return self._managed.get(inst_id)

    def all(self) -> list:
        return [self._managed[i.id] for i in self.cfg.instances
                if i.id in self._managed]

    # ── memory ────────────────────────────────────────────────────────
    @staticmethod
    def total_ram_gb() -> float:
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)

    def committed_gb(self) -> float:
        """Sum of the declared sizes of everything currently up. Uses the model
        file size, not RSS: RSS lags during load, and the guard has to answer
        before the memory is actually taken."""
        return round(sum(m.inst.size_gb for m in self.all()
                         if m.state in (RUNNING, STARTING)), 1)

    def used_gb(self) -> float:
        return round(sum(m.rss_gb() for m in self.all()), 1)

    def can_start(self, inst_id: str) -> tuple[bool, str]:
        m = self.get(inst_id)
        if m is None:
            return False, "no such instance"
        budget = self.total_ram_gb() - MEM_HEADROOM_GB
        planned = self.committed_gb() + m.inst.size_gb
        if planned > budget:
            return False, (f"would need {planned:.1f} GB of a {budget:.1f} GB "
                           f"budget ({self.total_ram_gb():.0f} GB installed, "
                           f"{MEM_HEADROOM_GB:.0f} GB reserved for the system)")
        if _port_busy(m.inst.port):
            return False, f"port {m.inst.port} is already in use"
        return True, ""

    # ── bulk operations ───────────────────────────────────────────────
    def start(self, inst_id: str) -> tuple[bool, str]:
        ok, why = self.can_start(inst_id)
        if not ok:
            return False, why
        return self._managed[inst_id].start()

    def stop(self, inst_id: str) -> None:
        m = self.get(inst_id)
        if m:
            m.stop()

    def stop_all(self) -> None:
        for m in self.all():
            m.stop()

    def start_all(self) -> list[tuple[str, bool, str]]:
        """Start every stopped instance, largest first so the memory budget is
        spent on the big models rather than exhausted by small ones."""
        out = []
        pending = [m for m in self.all() if m.state == IDLE]
        for m in sorted(pending, key=lambda x: -x.inst.size_gb):
            ok, msg = self.start(m.inst.id)
            out.append((m.inst.id, ok, msg))
        return out

    def restore(self) -> list[tuple[str, bool, str]]:
        """Bring back what was running before the last shutdown."""
        out = []
        for m in self.all():
            if m.inst.was_running and m.state == IDLE:
                ok, msg = self.start(m.inst.id)
                out.append((m.inst.id, ok, msg))
        return out

    def remember_running(self) -> None:
        for m in self.all():
            m.inst.was_running = m.state == RUNNING

    # ── reporting ─────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        rows = []
        for m in self.all():
            i = m.inst
            rows.append({
                "id": i.id,
                "name": i.name,
                "model_path": i.model_path,
                "host": i.host,
                "port": i.port,
                "ctx_size": i.ctx_size,
                "parallel": i.parallel,
                "size_gb": i.size_gb,
                "state": m.state,
                "detail": m.detail,
                "pid": m.pid,
                "uptime": m.uptime,
                "rss_gb": m.rss_gb(),
                "requests": m.requests_active(),
                "url": f"http://{_display_host(i.host)}:{i.port}",
            })
        return {
            "instances": rows,
            "running": sum(1 for r in rows if r["state"] == RUNNING),
            "total": len(rows),
            "committed_gb": self.committed_gb(),
            "used_gb": self.used_gb(),
            "total_ram_gb": self.total_ram_gb(),
            "budget_gb": round(self.total_ram_gb() - MEM_HEADROOM_GB, 1),
            "lan_ip": lan_ip(),
        }


# ── helpers ───────────────────────────────────────────────────────────
def _display_host(host: str) -> str:
    """0.0.0.0 is correct but tells nobody where to connect."""
    return lan_ip() if host in ("0.0.0.0", "::") else host


def lan_ip() -> str:
    """Best-effort local address. No packet is actually sent."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.1.1", 1))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def _port_busy(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            for n in range(LOG_BACKUPS - 1, 0, -1):
                older, newer = path.with_suffix(f".{n}"), path.with_suffix(f".{n+1}")
                if older.exists():
                    shutil.move(str(older), str(newer))
            shutil.move(str(path), str(path.with_suffix(".1")))
    except Exception as exc:
        log.debug("log rotation failed for %s: %s", path, exc)


def _last_lines(path: Path, n: int) -> str:
    try:
        return " / ".join(path.read_text(errors="replace").splitlines()[-n:])
    except Exception:
        return ""
