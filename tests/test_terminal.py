import platform
from collections.abc import Callable
from pathlib import Path

import pytest

from wt import proc
from wt.errors import WtError
from wt.terminal import open_terminal, terminal_command

PATH = Path("/work/trees/feat-cart")


def finder(*present: str) -> Callable[[str], str | None]:
    return lambda name: f"/usr/bin/{name}" if name in present else None


def command(env: dict[str, str], *present: str, system: str = "Linux") -> tuple[list[str], Path]:
    return terminal_command(PATH, env, finder(*present), system)


def test_wt_terminal_is_split_and_gets_the_path_in_each_word() -> None:
    argv, cwd = command({"WT_TERMINAL": "myterm --dir {path} --tab", "TERMINAL": "other"})
    assert argv == ["myterm", "--dir", str(PATH), "--tab"]
    assert cwd == PATH


def test_wt_terminal_keeps_a_path_with_spaces_as_one_word() -> None:
    spaced = Path("/work/my trees/feat")
    argv, _ = terminal_command(spaced, {"WT_TERMINAL": "myterm --dir {path}"}, finder(), "Linux")
    assert argv == ["myterm", "--dir", str(spaced)]


def test_wt_terminal_honors_shell_quoting() -> None:
    argv, _ = command({"WT_TERMINAL": "myterm -e 'cd {path} && zsh'"})
    assert argv == ["myterm", "-e", f"cd {PATH} && zsh"]


def test_terminal_variable_starts_in_the_folder_through_cwd() -> None:
    argv, cwd = command({"TERMINAL": "st -f mono"}, "kitty")
    assert (argv, cwd) == (["st", "-f", "mono"], PATH)


def test_blank_variables_are_ignored() -> None:
    argv, _ = command({"WT_TERMINAL": "  ", "TERMINAL": ""}, "xterm")
    assert argv == ["xterm"]


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("kitty", ["kitty", "--directory", str(PATH)]),
        ("konsole", ["konsole", "--workdir", str(PATH)]),
        ("gnome-terminal", ["gnome-terminal", f"--working-directory={PATH}"]),
        ("alacritty", ["alacritty", "--working-directory", str(PATH)]),
        ("wezterm", ["wezterm", "start", "--cwd", str(PATH)]),
        ("foot", ["foot", f"--working-directory={PATH}"]),
        ("xfce4-terminal", ["xfce4-terminal", f"--working-directory={PATH}"]),
        ("x-terminal-emulator", ["x-terminal-emulator"]),
        ("xterm", ["xterm"]),
    ],
)
def test_each_linux_terminal_gets_its_own_start_here_flag(name: str, argv: list[str]) -> None:
    assert command({}, name) == (argv, PATH)


def test_the_first_linux_terminal_on_the_list_wins() -> None:
    argv, _ = command({}, "xterm", "alacritty", "konsole", "foot")
    assert argv[0] == "konsole"


def test_macos_opens_the_terminal_app() -> None:
    assert command({}, system="Darwin") == (["open", "-a", "Terminal", str(PATH)], PATH)


def test_environment_wins_over_macos_default() -> None:
    argv, _ = command({"TERMINAL": "iterm-cli"}, system="Darwin")
    assert argv == ["iterm-cli"]


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_no_terminal_is_an_error_that_names_the_fix(system: str) -> None:
    with pytest.raises(WtError, match="no terminal found; set WT_TERMINAL"):
        command({}, system=system)


def test_a_broken_wt_terminal_line_is_an_error() -> None:
    with pytest.raises(WtError, match="WT_TERMINAL"):
        command({"WT_TERMINAL": "myterm 'unclosed {path}"})


def test_open_terminal_spawns_detached_from_the_real_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[tuple[list[str], Path | None]] = []
    monkeypatch.setenv("WT_TERMINAL", "myterm --dir {path}")
    monkeypatch.setattr(
        proc, "spawn_detached", lambda args, *, cwd=None: spawned.append((list(args), cwd))
    )
    open_terminal(PATH)
    assert spawned == [(["myterm", "--dir", str(PATH)], PATH)]


def test_open_terminal_finds_programs_with_proc_which(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.delenv("WT_TERMINAL", raising=False)
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(proc, "which", finder("foot"))
    monkeypatch.setattr(
        proc, "spawn_detached", lambda args, *, cwd=None: spawned.append(list(args))
    )
    open_terminal(PATH)
    assert spawned == [["foot", f"--working-directory={PATH}"]]
