"""Open a terminal window in a folder (SPEC §26): choose the program, then start it detached."""

import os
import platform
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path

from wt import proc
from wt.errors import WtError

PATH_PLACEHOLDER = "{path}"
OWN_COMMAND_VARIABLE = "WT_TERMINAL"  # a command line with `{path}`
STANDARD_VARIABLE = "TERMINAL"  # a program; started with the folder as its working directory
NO_TERMINAL = "no terminal found; set WT_TERMINAL"
LINUX = "Linux"
MACOS = "Darwin"
# Linux terminals in the order they are tried, each with the flags that start it in a folder.
# A terminal with no flags is started with the folder as its working directory only.
LINUX_TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("kitty", ("--directory", PATH_PLACEHOLDER)),
    ("konsole", ("--workdir", PATH_PLACEHOLDER)),
    ("gnome-terminal", (f"--working-directory={PATH_PLACEHOLDER}",)),
    ("alacritty", ("--working-directory", PATH_PLACEHOLDER)),
    ("wezterm", ("start", "--cwd", PATH_PLACEHOLDER)),
    ("foot", (f"--working-directory={PATH_PLACEHOLDER}",)),
    ("xfce4-terminal", (f"--working-directory={PATH_PLACEHOLDER}",)),
    ("x-terminal-emulator", ()),
    ("xterm", ()),
)


def _split(line: str, variable: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError as exc:
        raise WtError(f"{variable} is not a valid command line: {exc}") from exc


def _with_path(words: list[str], path: Path) -> list[str]:
    # Replaced word by word, after the split: a path with spaces stays one argument.
    return [word.replace(PATH_PLACEHOLDER, str(path)) for word in words]


def _from_environment(path: Path, env: Mapping[str, str]) -> list[str] | None:
    own = env.get(OWN_COMMAND_VARIABLE, "").strip()
    if own:
        return _with_path(_split(own, OWN_COMMAND_VARIABLE), path)
    standard = env.get(STANDARD_VARIABLE, "").strip()
    return _split(standard, STANDARD_VARIABLE) if standard else None


def _from_system(path: Path, which: Callable[[str], str | None], system: str) -> list[str] | None:
    if system == MACOS:
        return ["open", "-a", "Terminal", str(path)]
    if system == LINUX:
        for name, flags in LINUX_TERMINALS:
            if which(name):
                return [name, *_with_path(list(flags), path)]
    return None


def terminal_command(
    path: Path, env: Mapping[str, str], which: Callable[[str], str | None], system: str
) -> tuple[list[str], Path]:
    """The argv that opens a terminal in `path`, and the working directory to start it with.

    `WT_TERMINAL`, then `$TERMINAL`, then the first terminal of the system that `which` finds.
    """
    argv = _from_environment(path, env) or _from_system(path, which, system)
    if not argv:
        raise WtError(NO_TERMINAL)
    return argv, path


def open_terminal(path: Path) -> None:
    """Open a terminal in `path` and return at once (the window outlives wt)."""
    argv, cwd = terminal_command(path, os.environ, proc.which, platform.system())
    proc.spawn_detached(argv, cwd=cwd)
