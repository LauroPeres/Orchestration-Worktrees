"""Deterministic names. Everything is derived from (project, branch) so the same branch always
gets the same resources, and teardown never has to guess."""

import hashlib
import re
from dataclasses import dataclass

from wt.errors import WtError
from wt.paths import instance

MAX_SLUG = 40  # leaves room for prefixes in DNS labels (63) and Postgres identifiers (63)
MAX_LABEL = 63
PORT_BASE = 10000
PORT_STRIDE = 100  # services per worktree
MAX_PORT = 65535


def _short_hash(text: str, length: int = 6) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def fit(name: str, limit: int = MAX_LABEL) -> str:
    """Shorten a name to `limit` chars, keeping it unique with a hash suffix."""
    if len(name) <= limit:
        return name
    suffix = _short_hash(name)
    return f"{name[: limit - len(suffix) - 1].rstrip('-_')}-{suffix}"


def temp_name(name: str) -> str:
    """Scratch name for building `name` before renaming it. Lowercase `[a-z0-9_]`, max 63 chars,
    deterministic so a leftover from a killed run is found and dropped on the next one."""
    return f"{name[:50]}_{_short_hash(name)}_tmp"


def slugify(branch: str) -> str:
    """`feat/Cart_v2` -> `feat-cart-v2`. Safe for DNS labels and compose project names."""
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
    if not slug:
        raise WtError(f"branch name {branch!r} has no letters or digits to build a name from")
    if len(slug) > MAX_SLUG:
        slug = fit(slug, MAX_SLUG)
    return slug


def host_port(worktree_index: int, service_index: int) -> int:
    """The host port of a service: PORT_BASE + worktree_index * PORT_STRIDE + service_index."""
    if not 0 <= service_index < PORT_STRIDE:
        raise WtError(f"service index {service_index} is outside 0..{PORT_STRIDE - 1}")
    port = PORT_BASE + worktree_index * PORT_STRIDE + service_index
    if worktree_index < 0 or port > MAX_PORT:
        raise WtError(
            f"worktree index {worktree_index} gives port {port}, outside 1..{MAX_PORT}; "
            "too many worktrees"
        )
    return port


@dataclass(frozen=True)
class Names:
    project: str
    branch: str
    domain: str

    @property
    def slug(self) -> str:
        return slugify(self.branch)

    @property
    def ident(self) -> str:
        """Underscore form, for databases, consumer groups and key prefixes."""
        return self.slug.replace("-", "_")

    @property
    def branch_domain(self) -> str:
        return f"{self.slug}.{self.domain}"

    def host(self, subdomain: str) -> str:
        return f"{subdomain}.{self.branch_domain}" if subdomain else self.branch_domain

    def compose_project(self, repo: str) -> str:
        return fit(f"{instance()}-{self.project}-{self.slug}-{repo}")

    def network_alias(self, service: str) -> str:
        return fit(f"{instance()}-{self.project}-{self.slug}-{service}")

    @property
    def key(self) -> str:
        """Prefix for files that belong to this branch (route file, certs)."""
        return f"{self.project}--{self.slug}"

    def router(self, service: str) -> str:
        return f"{self.key}--{service}"


def service_ref(service: str, attr: str) -> str:
    """Template text for one attribute of a service.

    Always the subscript form: Jinja reads `services.a-b.url` as `services.a - b.url`, and
    service names may hold hyphens. `repr` quotes the name safely for a Jinja string literal.
    """
    return f"{{{{ services[{service!r}].{attr} }}}}"
