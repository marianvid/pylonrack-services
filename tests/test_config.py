"""Config round-trips, port allocation, atomic save."""
import json
import config as C


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SETTINGS_FILE", tmp_path / "nope.json")
    cfg = C.load()
    assert cfg.instances == []
    assert cfg.ui_port == 8768


def test_round_trip(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(C, "SETTINGS_FILE", f)
    cfg = C.AppConfig(llama_bin="/x/llama-server", hf_cache="/models")
    cfg.instances = [C.Instance(id="a1", name="M", model_path="/m.gguf",
                                port=8771, ctx_size=4096, size_gb=3.2)]
    C.save(cfg)
    back = C.load()
    assert back.llama_bin == "/x/llama-server"
    assert len(back.instances) == 1
    assert back.instances[0].ctx_size == 4096
    assert back.instances[0].size_gb == 3.2


def test_unknown_keys_ignored(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({
        "llama_bin": "/b", "future_flag": True,
        "instances": [{"id": "z", "name": "n", "model_path": "/p",
                       "port": 8771, "brand_new_field": 42}],
    }))
    monkeypatch.setattr(C, "SETTINGS_FILE", f)
    cfg = C.load()
    assert cfg.instances[0].id == "z"


def test_next_free_port_skips_taken():
    cfg = C.AppConfig()
    cfg.instances = [C.Instance(id="a", name="a", model_path="/a", port=8771),
                     C.Instance(id="b", name="b", model_path="/b", port=8772)]
    assert cfg.next_free_port() == 8773


def test_next_free_port_raises_when_full():
    cfg = C.AppConfig()
    cfg.instances = [C.Instance(id=str(p), name="x", model_path="/x", port=p)
                     for p in range(C.PORT_RANGE_START, C.PORT_RANGE_END + 1)]
    try:
        cfg.next_free_port()
        assert False, "should have raised"
    except RuntimeError:
        pass


def test_port_conflict_excludes_self():
    cfg = C.AppConfig()
    cfg.instances = [C.Instance(id="a", name="a", model_path="/a", port=8771)]
    assert cfg.port_conflict(8771) is True
    assert cfg.port_conflict(8771, exclude_id="a") is False


def test_save_is_atomic_no_tmp_left(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(C, "SETTINGS_FILE", f)
    C.save(C.AppConfig())
    assert f.exists()
    assert not list(tmp_path.glob("*.tmp"))
