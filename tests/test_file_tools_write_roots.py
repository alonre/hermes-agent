"""Tests for the file_tools.allowed_write_roots hard guard.

When a profile's config.yaml sets ``file_tools.allowed_write_roots``,
``write_file``/``patch`` must refuse any target outside the listed roots —
non-overridable from the tool call (unlike the soft cross-profile guard).
Absent/empty config keeps the tools unrestricted (backwards compatible).
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def file_tools_mod():
    import tools.file_tools as ft
    # reset the module-level cache between tests
    ft._allowed_write_roots_cached = None
    ft._allowed_write_roots_loaded = False
    yield ft
    ft._allowed_write_roots_cached = None
    ft._allowed_write_roots_loaded = False


def _set_roots(monkeypatch, ft, roots):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"file_tools": {"allowed_write_roots": roots}},
    )
    ft._allowed_write_roots_cached = None
    ft._allowed_write_roots_loaded = False


def test_unrestricted_when_unset(monkeypatch, file_tools_mod):
    _set_roots(monkeypatch, file_tools_mod, [])
    assert file_tools_mod._check_allowed_write_roots("/anywhere/at/all.txt") is None


def test_inside_root_allowed(monkeypatch, file_tools_mod, tmp_path):
    _set_roots(monkeypatch, file_tools_mod, [str(tmp_path)])
    assert file_tools_mod._check_allowed_write_roots(str(tmp_path / "a" / "b.md")) is None


def test_outside_root_refused(monkeypatch, file_tools_mod, tmp_path):
    _set_roots(monkeypatch, file_tools_mod, [str(tmp_path / "vault")])
    err = file_tools_mod._check_allowed_write_roots(str(tmp_path / "elsewhere" / "x.py"))
    assert err is not None and "allowed write roots" in err


def test_traversal_out_of_root_refused(monkeypatch, file_tools_mod, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    _set_roots(monkeypatch, file_tools_mod, [str(root)])
    sneaky = str(root / ".." / "escape.txt")
    err = file_tools_mod._check_allowed_write_roots(sneaky)
    assert err is not None


def test_tilde_root_expands(monkeypatch, file_tools_mod, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _set_roots(monkeypatch, file_tools_mod, ["~/vault"])
    assert file_tools_mod._check_allowed_write_roots(str(tmp_path / "vault" / "n.md")) is None
    assert file_tools_mod._check_allowed_write_roots(str(tmp_path / "other.md")) is not None


@pytest.fixture()
def home_tmp():
    # pytest's tmp_path lives under /private/var on macOS, which trips the
    # pre-existing sensitive-path guard; integration tests need a home-based dir.
    import shutil
    import uuid
    d = Path.home() / f".wr-guard-test-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_write_file_tool_blocks(monkeypatch, file_tools_mod, home_tmp):
    root = home_tmp / "vault"
    root.mkdir()
    _set_roots(monkeypatch, file_tools_mod, [str(root)])
    out = file_tools_mod.write_file_tool(str(home_tmp / "outside.txt"), "nope",
                                         task_id="wr-test")
    assert "Refusing to write outside" in str(out)
    assert not (home_tmp / "outside.txt").exists()


def test_write_file_tool_allows_inside(monkeypatch, file_tools_mod, home_tmp):
    root = home_tmp / "vault"
    root.mkdir()
    _set_roots(monkeypatch, file_tools_mod, [str(root)])
    target = root / "ok.txt"
    out = file_tools_mod.write_file_tool(str(target), "fine", task_id="wr-test2")
    assert "Refusing" not in str(out)
    assert target.read_text() == "fine"
