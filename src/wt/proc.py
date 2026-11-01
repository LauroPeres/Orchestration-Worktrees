"""Thin wrapper over subprocess so the rest of the code (and the tests) see one interface."""

import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
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


class CommandTimeout(WtError):
    """The command ran past its timeout and was killed."""

    def __init__(self, args: Sequence[str], timeout: float | None) -> None:
        super().__init__(f"command timed out after {timeout}s: {' '.join(args)}")


def _child_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    """The extra variables go through the environment, never argv, which any user can read in ps."""
    return None if env is None else {**os.environ, **env}


def run(  # noqa: PLR0913
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    timeout: float | None = None,
    on_line: Callable[[str], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    """Run a command. With capture=False the output goes straight to the terminal.

    `env` is added to the inherited environment for the child (secrets belong here, not in args).

    With capture=False and `on_line`, stdout and stderr are merged and each line is passed to
    `on_line` (without the newline) instead of the terminal; `Result.stdout` holds the output.
    """
    if on_line is not None and not capture:
        streamed = _run_streaming(
            args, cwd=cwd, timeout=timeout, on_line=on_line, env=_child_env(env)
        )
        return _checked(args, streamed, check)
    try:
        proc = subprocess.run(
            list(args),
            cwd=cwd,
            env=_child_env(env),
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
        raise CommandTimeout(args, timeout) from exc
    except OSError as exc:
        raise WtError(f"could not run {' '.join(args)}: {exc}") from exc
    return _checked(args, Result(proc.returncode, proc.stdout or "", proc.stderr or ""), check)


def _checked(args: Sequence[str], result: Result, check: bool) -> Result:
    if check and not result.ok:
        raise CommandError(args, result)
    return result


def _run_streaming(  # noqa: C901
    args: Sequence[str],
    *,
    cwd: Path | None,
    timeout: float | None,
    on_line: Callable[[str], None],
    env: dict[str, str] | None,
) -> Result:
    try:
        child = subprocess.Popen(
            list(args),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,  # so a timeout can kill grandchildren holding the pipe
        )
    except FileNotFoundError as exc:
        if cwd is not None and not cwd.is_dir():
            raise WtError(f"folder does not exist: {cwd}") from exc
        if exc.filename == args[0]:
            raise WtError(f"'{args[0]}' was not found on PATH") from exc
        raise WtError(f"could not run {' '.join(args)}: {exc}") from exc
    except OSError as exc:
        raise WtError(f"could not run {' '.join(args)}: {exc}") from exc
    timer = None
    timed_out = threading.Event()
    if timeout is not None:

        def expire() -> None:
            timed_out.set()
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                child.kill()

        timer = threading.Timer(timeout, expire)
        timer.start()
    lines: list[str] = []
    try:
        assert child.stdout is not None
        for raw in child.stdout:
            line = raw.rstrip("\r\n")
            lines.append(line)
            on_line(line)
        child.wait()
    finally:
        if timer is not None:
            timer.cancel()
        if child.poll() is None:
            child.kill()
            child.wait()
    if timed_out.is_set():
        raise CommandTimeout(args, timeout)
    return Result(child.returncode, "".join(f"{line}\n" for line in lines), "")


def which(name: str) -> str | None:
    return shutil.which(name)
