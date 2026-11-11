import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from wt import disk, proc
from wt.errors import WtError

HEADER = "     Total   Exclusive  Set shared  Filename\n"


def fake_btrfs(
    monkeypatch: pytest.MonkeyPatch, result: proc.Result | WtError
) -> list[tuple[Sequence[str], dict[str, object]]]:
    """Replace proc.run; returns the recorded (args, kwargs) calls."""
    calls: list[tuple[Sequence[str], dict[str, object]]] = []

    def run(args: Sequence[str], **kwargs: object) -> proc.Result:
        calls.append((args, kwargs))
        if isinstance(result, WtError):
            raise result
        return result

    monkeypatch.setattr(proc, "run", run)
    return calls


def row(
    name: str, total: int = 140238848, exclusive: int = 10543104, shared: int = 105525248
) -> str:
    return f" {total:>9} {exclusive:>11} {shared:>11}  {name}\n"


def test_usage_parses_btrfs_du_and_sums_apparent_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 23)
    calls = fake_btrfs(monkeypatch, proc.Result(0, HEADER + row(str(tmp_path)), ""))

    assert disk.usage(tmp_path) == disk.DiskUsage(123, 10543104, 105525248)
    args, kwargs = calls[0]
    assert list(args) == ["btrfs", "filesystem", "du", "-s", "--raw", str(tmp_path)]
    assert kwargs["check"] is False
    assert isinstance(kwargs["timeout"], float) and kwargs["timeout"] >= 60


def test_usage_parses_a_path_with_spaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "my project" / "feat v1"
    folder.mkdir(parents=True)
    fake_btrfs(monkeypatch, proc.Result(0, HEADER + row(str(folder), 9, 4, 5), ""))

    result = disk.usage(folder)

    assert (result.exclusive_bytes, result.shared_bytes) == (4, 5)


def test_a_missing_btrfs_binary_gives_unknown_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "f").write_bytes(b"abc")
    fake_btrfs(monkeypatch, WtError("'btrfs' was not found on PATH"))

    assert disk.usage(tmp_path) == disk.DiskUsage(3, None, None)


def test_a_timeout_gives_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_btrfs(monkeypatch, proc.CommandTimeout(["btrfs"], 600.0))

    assert disk.usage(tmp_path) == disk.DiskUsage(0, None, None)


def test_a_non_zero_exit_gives_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_btrfs(monkeypatch, proc.Result(1, "", "ERROR: not a btrfs filesystem"))

    assert disk.usage(tmp_path) == disk.DiskUsage(0, None, None)


@pytest.mark.parametrize(
    "output",
    [
        "",
        HEADER,
        "garbage\nmore garbage here\n",
        HEADER + " 10 x 30  /some/path\n",
        HEADER + " 10 20\n",  # a row without columns for the name
        HEADER + " -10 20 30  /some/path\n",
    ],
)
def test_unparsable_output_gives_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    fake_btrfs(monkeypatch, proc.Result(0, output, ""))

    result = disk.usage(tmp_path)

    assert (result.exclusive_bytes, result.shared_bytes) == (None, None)


def test_a_missing_path_is_an_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_btrfs(monkeypatch, proc.Result(0, "", ""))
    missing = tmp_path / "gone"

    with pytest.raises(WtError, match=str(missing)):
        disk.usage(missing)
    assert calls == []


def test_a_file_path_is_an_error_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_btrfs(monkeypatch, proc.Result(0, "", ""))
    file = tmp_path / "f.txt"
    file.write_text("x")

    with pytest.raises(WtError, match=r"f\.txt"):
        disk.usage(file)


def test_apparent_bytes_never_follow_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"z" * 100_000)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "small.txt").write_bytes(b"12345")
    os.symlink(outside / "big.bin", tree / "link-to-file")
    os.symlink(outside, tree / "link-to-folder")
    fake_btrfs(monkeypatch, WtError("no btrfs"))

    size = disk.usage(tree).apparent_bytes

    # the file link counts as a link; a folder link is not descended into (same rule as before)
    assert size == 5 + os.lstat(tree / "link-to-file").st_size
    assert size < 1000


def test_an_empty_folder_is_zero_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_btrfs(monkeypatch, WtError("no btrfs"))

    assert disk.usage(tmp_path).apparent_bytes == 0
