import shutil
import subprocess
import zipfile
from importlib import resources
from pathlib import Path

import pytest

from wt.errors import WtError
from wt.paths import cache_dir, example_dir, installs_dir, tool_cache_dir, wt_home

REPO_ROOT = Path(__file__).resolve().parents[1]
WHEEL_EXAMPLE_FILES = (
    "wt/_examples/shop/bootstrap.sh",
    "wt/_examples/shop/catalog.yaml",
    "wt/_examples/shop/api/app.py",
    "wt/_examples/shop/web/server.py",
    "wt/_examples/shop/feat-cart/app.py",
)


def test_cache_dirs_live_under_wt_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_HOME", str(tmp_path))
    assert wt_home() == tmp_path
    assert cache_dir() == tmp_path / "cache"
    assert installs_dir() == tmp_path / "cache" / "installs"
    assert tool_cache_dir("uv") == tmp_path / "cache" / "uv"
    assert tool_cache_dir("npm") == tmp_path / "cache" / "npm"


class _FakePackage:
    """Stands in for `resources.files("wt")` so a test controls the packaged location."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __call__(self, package: str) -> Path:
        assert package == "wt"
        return self.root


def test_example_dir_falls_back_to_the_repo_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "files", _FakePackage(tmp_path))  # no _examples here
    assert example_dir("shop") == REPO_ROOT / "examples" / "shop"
    assert (example_dir("shop") / "catalog.yaml").is_file()


def test_example_dir_prefers_the_packaged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "_examples" / "shop"
    packaged.mkdir(parents=True)
    monkeypatch.setattr(resources, "files", _FakePackage(tmp_path))
    assert example_dir("shop") == packaged


def test_example_dir_names_both_places_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "files", _FakePackage(tmp_path))
    with pytest.raises(WtError) as raised:
        example_dir("nope")
    message = str(raised.value)
    assert "example 'nope' not found" in message
    assert str(tmp_path / "_examples" / "nope") in message
    assert str(REPO_ROOT / "examples" / "nope") in message


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_wheel_carries_the_shop_example_and_source_tree_stays_clean(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert set(WHEEL_EXAMPLE_FILES) <= names
    assert not (REPO_ROOT / "src" / "wt" / "_examples").exists()
