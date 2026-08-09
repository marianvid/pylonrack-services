"""The command layer — one implementation shared by the rack and the page."""
import asyncio
import config as C
import server as S


def _state(tmp_path, monkeypatch, *insts):
    monkeypatch.setattr(C, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "llama-server").write_text("#!/bin/sh\n")
    st = S.AppState()
    st.cfg.llama_bin = str(tmp_path / "llama-server")
    st.cfg.log_dir = str(tmp_path / "logs")
    st.cfg.hf_cache = str(tmp_path / "cache")
    st.cfg.instances = list(insts)
    st.mgr.cfg = st.cfg
    st.mgr.sync()
    return st


def _model(tmp_path, name="m", port=8771):
    p = tmp_path / f"{name}.gguf"
    p.write_bytes(b"x" * 1024)
    return C.Instance(id=name, name=name, model_path=str(p), port=port, size_gb=0.1)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_snapshot_action(tmp_path, monkeypatch):
    cmd = S.Commands(_state(tmp_path, monkeypatch, _model(tmp_path)))
    r = run(cmd.run("snapshot", {}))
    assert r["ok"] and r["snapshot"]["total"] == 1


def test_unknown_action_is_reported(tmp_path, monkeypatch):
    cmd = S.Commands(_state(tmp_path, monkeypatch))
    r = run(cmd.run("nonsense", {}))
    assert r["ok"] is False and "unknown action" in r["error"]


def test_add_rejects_missing_file(tmp_path, monkeypatch):
    cmd = S.Commands(_state(tmp_path, monkeypatch))
    r = run(cmd.run("add", {"path": str(tmp_path / "nope.gguf")}))
    assert r["ok"] is False and "not a file" in r["error"]


def test_add_then_duplicate_rejected(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch)
    cmd = S.Commands(st)
    p = tmp_path / "x.gguf"
    p.write_bytes(b"y" * 2048)
    assert run(cmd.run("add", {"path": str(p)}))["ok"] is True
    assert len(st.cfg.instances) == 1
    r = run(cmd.run("add", {"path": str(p)}))
    assert r["ok"] is False and "already" in r["error"]


def test_add_assigns_first_free_port(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path, "a", C.PORT_RANGE_START))
    cmd = S.Commands(st)
    p = tmp_path / "b.gguf"
    p.write_bytes(b"z")
    run(cmd.run("add", {"path": str(p)}))
    assert st.cfg.instances[-1].port == C.PORT_RANGE_START + 1


def test_update_rejects_port_conflict(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch,
                _model(tmp_path, "a", 8771), _model(tmp_path, "b", 8772))
    cmd = S.Commands(st)
    r = run(cmd.run("update", {"id": "b", "fields": {"port": 8771}}))
    assert r["ok"] is False and "used by another" in r["error"]


def test_update_rejects_tiny_context(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path))
    cmd = S.Commands(st)
    r = run(cmd.run("update", {"id": "m", "fields": {"ctx_size": 10}}))
    assert r["ok"] is False and "context" in r["error"]


def test_update_coerces_types(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path))
    cmd = S.Commands(st)
    r = run(cmd.run("update", {"id": "m", "fields": {
        "ctx_size": "16384", "parallel": "4", "temperature": "0.5",
        "flash_attn": False}}))
    assert r["ok"] is True
    i = st.cfg.find("m")
    assert i.ctx_size == 16384 and i.parallel == 4
    assert i.temperature == 0.5 and i.flash_attn is False


def test_update_rejects_garbage(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path))
    cmd = S.Commands(st)
    r = run(cmd.run("update", {"id": "m", "fields": {"ctx_size": "many"}}))
    assert r["ok"] is False and "bad value" in r["error"]


def test_remove_deletes_instance(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path))
    cmd = S.Commands(st)
    assert run(cmd.run("remove", {"id": "m"}))["ok"] is True
    assert st.cfg.instances == []


def test_relocate_requires_real_file(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path))
    cmd = S.Commands(st)
    r = run(cmd.run("relocate", {"id": "m", "path": "/nowhere.gguf"}))
    assert r["ok"] is False


def test_relocate_updates_path_and_size(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path))
    cmd = S.Commands(st)
    new = tmp_path / "moved.gguf"
    new.write_bytes(b"q" * 4096)
    assert run(cmd.run("relocate", {"id": "m", "path": str(new)}))["ok"] is True
    assert st.cfg.find("m").model_path == str(new)


def test_set_option_unknown_key(tmp_path, monkeypatch):
    cmd = S.Commands(_state(tmp_path, monkeypatch))
    r = run(cmd.run("set_option", {"key": "nope", "value": True}))
    assert r["ok"] is False


def test_status_text_reflects_state(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path))
    assert S._status_text(st) == "Idle"
    st.busy = "Starting…"
    assert S._status_text(st) == "Starting…"


def test_manifest_declares_one_mode(tmp_path, monkeypatch):
    """The log lives inside the body, where it can be filtered per instance,
    so the rack only needs to show the one panel."""
    st = _state(tmp_path, monkeypatch)
    m = S._manifest(st)
    assert m["modes"] == ["instances"]
    assert m["ui_url"].endswith(f":{st.cfg.ui_port}/")


def test_log_merged_tags_each_line(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path, "alpha"))
    st.cfg.log_file_for("alpha").write_text("one\ntwo\n", encoding="utf-8")
    r = run(S.Commands(st).run("log", {}))
    assert r["ok"] and all(l.startswith("alpha") for l in r["lines"])


def test_log_single_instance_is_untagged(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch, _model(tmp_path, "alpha"))
    st.cfg.log_file_for("alpha").write_text("one\ntwo\n", encoding="utf-8")
    r = run(S.Commands(st).run("log", {"id": "alpha"}))
    assert r["lines"] == ["one", "two"]


def test_manifest_controls_present(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch)
    ids = {c["id"] for c in S._manifest(st)["controls"]}
    assert {"start_all", "stop_all", "status_label", "node_label"} <= ids
