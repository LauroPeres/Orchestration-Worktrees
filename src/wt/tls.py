"""Local TLS certificates with mkcert.

One certificate per branch covers the branch domain and its subdomains. A `*.shop.localhost`
wildcard would not cover `api.feat-cart.shop.localhost`.
"""

from pathlib import Path

from wt import proc
from wt.naming import Names
from wt.router import certs_dir


def available() -> bool:
    return proc.which("mkcert") is not None


def caroot() -> Path | None:
    if not available():
        return None
    out = proc.run(["mkcert", "-CAROOT"], check=False).stdout.strip()
    return Path(out) if out else None


def ca_ready() -> bool:
    root = caroot()
    return root is not None and (root / "rootCA.pem").is_file()


def install_ca() -> None:
    proc.run(["mkcert", "-install"], capture=False)


def cert_paths(names: Names) -> tuple[Path, Path]:
    return certs_dir() / f"{names.key}.pem", certs_dir() / f"{names.key}-key.pem"


def ensure_cert(names: Names) -> tuple[Path, Path] | None:
    """Create the branch certificate if mkcert is installed. None means: no local CA."""
    cert, key = cert_paths(names)
    if cert.is_file() and key.is_file():
        return cert, key
    if not available():
        return None
    certs_dir().mkdir(parents=True, exist_ok=True)
    proc.run(
        [
            "mkcert",
            "-cert-file",
            str(cert),
            "-key-file",
            str(key),
            names.branch_domain,
            f"*.{names.branch_domain}",
        ]
    )
    return cert, key


def remove_cert(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
