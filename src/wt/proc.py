"""Thin wrapper over subprocess so the rest of the code (and the tests) see one interface."""

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from wt.errors import WtError


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(WtError):
    def __init__(self, args: Sequence[str], result: Result) -> None:
        self.result = result
        detail = (result.stderr or result.stdout).strip()
        tail = "\n".join(detail.splitlines()[-15:])
        super().__init__(f"command failed ({result.returncode}): {' '.join(args)}\n{tail}")


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    timeout: float | None = None,
) -> Result:
    """Run a command. With capture=False the output goes straight to the terminal."""
    try:
        proc = subprocess.run(
            list(args),
            cwd=cwd,
            capture_output=capture,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        if cwd is not None and not cwd.is_dir():
            raise WtError(f"folder does not exist: {cwd}") from exc
        if exc.filename == args[0]:
            raise WtError(f"'{args[0]}' was not found on PATH") from exc
        raise WtError(f"could not run {' '.join(args)}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WtError(f"command timed out after {timeout}s: {' '.join(args)}") from exc
    except OSError as exc:
        raise WtError(f"could not run {' '.join(args)}: {exc}") from exc
    result = Result(proc.returncode, proc.stdout or "", proc.stderr or "")
    if check and not result.ok:
        raise CommandError(args, result)
    return result


def which(name: str) -> str | None:
    return shutil.which(name)
