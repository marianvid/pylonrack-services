"""Instance state derivation, the memory guard, and the command line."""
import config as C
import instances as I


def _cfg(tmp_path, *insts):
    cfg = C.AppConfig(llama_bin=str(tmp_path / "llama-server"),
                      log_dir=str(tmp_path / "logs"))
    (tmp_path / "llama-server").write_text("#!/bin/sh\n")
    cfg.instances = list(insts)
    return cfg


def _inst(tmp_path, name="m", port=8771, size=2.0, exists=True):
    p = tmp_path / f"{name}.gguf"
    if exists:
        p.write_bytes(b"x")
    return C.Instance(id=name, name=name, model_path=str(p),
                      port=port, size_gb=size)


def test_missing_when_file_absent(tmp_path):
    inst = _inst(tmp_path, exists=False)
    mgr = I.InstanceManager(_cfg(tmp_path, inst))
    assert mgr.get("m").state == I.MISSING


def test_idle_when_file_present(tmp_path):
    mgr = I.InstanceManager(_cfg(tmp_path, _inst(tmp_path)))
    assert mgr.get("m").state == I.IDLE


def test_state_reacts_to_file_disappearing(tmp_path):
    inst = _inst(tmp_path)
    mgr = I.InstanceManager(_cfg(tmp_path, inst))
    assert mgr.get("m").state == I.IDLE
    (tmp_path / "m.gguf").unlink()
    assert mgr.get("m").state == I.MISSING


def test_memory_guard_blocks_oversized(tmp_path, monkeypatch):
    huge = _inst(tmp_path, size=10_000.0)
    mgr = I.InstanceManager(_cfg(tmp_path, huge))
    ok, why = mgr.can_start("m")
    assert ok is False and "budget" in why


def test_memory_guard_allows_small(tmp_path):
    mgr = I.InstanceManager(_cfg(tmp_path, _inst(tmp_path, size=0.1)))
    ok, _ = mgr.can_start("m")
    assert ok is True


def test_start_refuses_missing_file(tmp_path):
    mgr = I.InstanceManager(_cfg(tmp_path, _inst(tmp_path, exists=False)))
    ok, msg = mgr.get("m").start()
    assert ok is False and "not found" in msg


def test_command_contains_every_parameter(tmp_path):
    inst = _inst(tmp_path)
    inst.ctx_size, inst.parallel, inst.api_key = 4096, 3, "secret"
    inst.flash_attn, inst.mlock = True, True
    mgr = I.InstanceManager(_cfg(tmp_path, inst))
    cmd = " ".join(mgr.get("m").build_command())
    for frag in ("--host 0.0.0.0", "--port 8771", "--ctx-size 4096",
                 "--parallel 3", "--flash-attn on", "--mlock",
                 "--api-key secret"):
        assert frag in cmd, frag


def test_no_api_key_flag_when_empty(tmp_path):
    mgr = I.InstanceManager(_cfg(tmp_path, _inst(tmp_path)))
    assert "--api-key" not in " ".join(mgr.get("m").build_command())


def test_sync_drops_removed_instances(tmp_path):
    a, b = _inst(tmp_path, "a", 8771), _inst(tmp_path, "b", 8772)
    cfg = _cfg(tmp_path, a, b)
    mgr = I.InstanceManager(cfg)
    assert len(mgr.all()) == 2
    cfg.instances = [a]
    mgr.sync()
    assert [m.inst.id for m in mgr.all()] == ["a"]


def test_snapshot_shape(tmp_path):
    mgr = I.InstanceManager(_cfg(tmp_path, _inst(tmp_path)))
    s = mgr.snapshot()
    for k in ("instances", "running", "total", "committed_gb",
              "total_ram_gb", "budget_gb", "lan_ip"):
        assert k in s
    assert s["instances"][0]["state"] == I.IDLE


def test_display_host_resolves_wildcard(tmp_path):
    inst = _inst(tmp_path)
    inst.host = "0.0.0.0"
    mgr = I.InstanceManager(_cfg(tmp_path, inst))
    assert "0.0.0.0" not in mgr.snapshot()["instances"][0]["url"]


def test_remember_running_marks_state(tmp_path):
    inst = _inst(tmp_path)
    inst.was_running = True
    mgr = I.InstanceManager(_cfg(tmp_path, inst))
    mgr.remember_running()
    assert inst.was_running is False        # nothing is actually running
