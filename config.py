"""config.py — persisted configuration for the services slot.

Everything the slot knows lives in settings.json next to this file: the
llama.cpp binary path, the model cache location, and one entry per managed
instance. The file is the single source of truth and is rewritten atomically,
so a crash mid-write cannot leave a truncated file that loses every instance.

Why instance state is persisted: the Mac is an inference node for the home
lab. Whatever was running before a reboot has to come back without anyone
opening anything, so `was_running` is saved alongside the parameters.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

SETTINGS_FILE = Path(__file__).parent / "settings.json"

# 8766 is the rack channel and 8768 the UI server, so instances start above.
PORT_RANGE_START = 8771
PORT_RANGE_END = 8829


@dataclass
class Instance:
    """One managed llama-server process."""

    id: str
    name: str
    model_path: str
    port: int
    host: str = "0.0.0.0"
    ctx_size: int = 8192
    parallel: int = 1
    threads: int = 8
    n_gpu_layers: int = 99
    batch_size: int = 512
    ubatch_size: int = 256
    flash_attn: bool = True
    mlock: bool = False
    temperature: float = 0.8
    top_p: float = 0.95
    top_k: int = 40
    repeat_penalty: float = 1.1
    api_key: str = ""
    draft_model_path: str = ""
    extra_args: str = ""      # anything llama-server accepts that is not modelled
    size_gb: float = 0.0
    was_running: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Instance":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class AppConfig:
    llama_bin: str = "/usr/local/bin/llama-server"
    hf_cache: str = "~/.cache/huggingface/hub"
    log_dir: str = "~/.pylonrack/services"
    ui_port: int = 8768
    start_rack_at_login: bool = False
    load_slot_automatically: bool = True
    instances: list = field(default_factory=list)

    # ── resolved paths ────────────────────────────────────────────────
    @property
    def bin_path(self) -> Path:
        return Path(os.path.expanduser(self.llama_bin))

    @property
    def hf_cache_path(self) -> Path:
        return Path(os.path.expanduser(self.hf_cache))

    @property
    def log_path(self) -> Path:
        p = Path(os.path.expanduser(self.log_dir))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def log_file_for(self, inst_id: str) -> Path:
        return self.log_path / f"{inst_id}.log"

    # ── instance helpers ──────────────────────────────────────────────
    def find(self, inst_id: str):
        return next((i for i in self.instances if i.id == inst_id), None)

    def next_free_port(self) -> int:
        """First unused port in range. Raises when the range is full rather
        than silently reusing one — two servers on one port means the second
        dies with an error nobody reads."""
        taken = {i.port for i in self.instances}
        for p in range(PORT_RANGE_START, PORT_RANGE_END + 1):
            if p not in taken:
                return p
        raise RuntimeError(
            f"no free port between {PORT_RANGE_START} and {PORT_RANGE_END}")

    def port_conflict(self, port: int, exclude_id: str = "") -> bool:
        return any(i.port == port and i.id != exclude_id for i in self.instances)


def load() -> AppConfig:
    """Read settings.json. Missing keys fall back to defaults; unknown keys are
    ignored, so a file written by a newer build still loads."""
    if not SETTINGS_FILE.exists():
        return AppConfig()

    raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    cfg = AppConfig(
        llama_bin=raw.get("llama_bin", AppConfig.llama_bin),
        hf_cache=raw.get("hf_cache", AppConfig.hf_cache),
        log_dir=raw.get("log_dir", AppConfig.log_dir),
        ui_port=raw.get("ui_port", AppConfig.ui_port),
        start_rack_at_login=raw.get("start_rack_at_login", False),
        load_slot_automatically=raw.get("load_slot_automatically", True),
    )
    cfg.instances = [Instance.from_dict(d) for d in raw.get("instances", [])]
    return cfg


def save(cfg: AppConfig) -> None:
    """Atomic write. A crash halfway through must not destroy the config."""
    payload = {
        "llama_bin": cfg.llama_bin,
        "hf_cache": cfg.hf_cache,
        "log_dir": cfg.log_dir,
        "ui_port": cfg.ui_port,
        "start_rack_at_login": cfg.start_rack_at_login,
        "load_slot_automatically": cfg.load_slot_automatically,
        "instances": [i.to_dict() for i in cfg.instances],
    }
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SETTINGS_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
