from pathlib import Path

import pytest

from wt.paths import cache_dir, installs_dir, tool_cache_dir, wt_home


def test_cache_dirs_live_under_wt_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_HOME", str(tmp_path))
    assert wt_home() == tmp_path
    assert cache_dir() == tmp_path / "cache"
    assert installs_dir() == tmp_path / "cache" / "installs"
    assert tool_cache_dir("uv") == tmp_path / "cache" / "uv"
    assert tool_cache_dir("npm") == tmp_path / "cache" / "npm"
