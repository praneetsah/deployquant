"""DATA_ROOT has one owner (spec §6): the open config module. The private
api used to define it in main.py and four modules imported it from there —
a dependency from the engine's sandbox runner onto the FastAPI app."""
import os

from dqengine import config


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DQENGINE_DATA_ROOT", str(tmp_path))
    assert config.data_root() == str(tmp_path)


def test_source_checkout_resolves_to_repo_qc_data(monkeypatch):
    monkeypatch.delenv("DQENGINE_DATA_ROOT", raising=False)
    here = os.path.dirname(os.path.abspath(config.__file__))          # .../platform/engine/dqengine
    expected = os.path.abspath(os.path.join(here, "..", "..", "..", "qc", "data"))
    if os.path.isdir(expected):
        assert config.data_root() == expected
    else:
        assert config.data_root() == os.path.abspath("data")


def test_installed_wheel_falls_back_to_cwd_data(monkeypatch, tmp_path):
    monkeypatch.delenv("DQENGINE_DATA_ROOT", raising=False)
    monkeypatch.setattr(config, "_checkout_data_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    assert config.data_root() == str(tmp_path / "data")


def test_module_constant_matches_resolver():
    assert config.DATA_ROOT == config.data_root() or "DQENGINE_DATA_ROOT" in os.environ
