"""A minimal `wt` provider plugin: the slice of a branch is a line in a file."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from wt.config import Config
from wt.providers.base import BranchContext, Check, Kind, Resources


class EchoConfig(BaseModel):
    """The mapping under `infra: echo:` in the catalog."""

    model_config = ConfigDict(extra="forbid")

    file: str


class EchoProvider:
    name = "echo"
    config_model = EchoConfig

    def __init__(self, cfg: Config, log: Callable[[str], None]) -> None:
        self.file = Path(EchoConfig.model_validate(cfg.shared.extra[self.name]).file)
        self.log = log

    def ensure_server(self) -> None:
        """No shared server: the file is the whole resource."""

    def preflight(self, ctx: BranchContext) -> None:
        """Nothing to refuse."""

    def create(self, ctx: BranchContext) -> Resources:
        self._append(f"create {ctx.names.slug}")
        return Resources([ctx.names.slug])

    def verify(self, ctx: BranchContext) -> Check:
        if self.file.exists():
            return Check(True, f"{self.file} exists", [])
        return Check(False, f"{self.file} is missing", [])

    def destroy(self, ctx: BranchContext) -> None:
        self._append(f"destroy {ctx.names.slug}")

    def template_vars(self, ctx: BranchContext, kind: Kind) -> dict[str, Any]:
        return {self.name: {"file": str(self.file)}}

    def _append(self, line: str) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with self.file.open("a") as handle:
            handle.write(f"{line}\n")
        self.log(line)
