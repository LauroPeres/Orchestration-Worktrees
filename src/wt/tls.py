"""Local TLS certificates with mkcert.

One certificate per branch covers the branch domain and its subdomains. A `*.shop.localhost`
wildcard would not cover `api.feat-cart.shop.localhost`.
"""

import sys
from collections.abc import Callable
from pathlib import Path

from wt import proc
from wt.errors import WtError
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


# Folders where Linux distributions read extra trusted roots (Arch, Debian/Ubuntu, Fedora/RHEL).
_ANCHOR_DIRS = (
    Path("/etc/ca-certificates/trust-source/anchors"),
    Path("/usr/local/share/ca-certificates"),
    Path("/etc/pki/ca-trust/source/anchors"),
    Path("/usr/share/ca-certificates/mkcert"),
)
_CA_LABEL = "mkcert"
_MACOS_KEYCHAINS = (
    "/Library/Keychains/System.keychain",
    "~/Library/Keychains/login.keychain-db",
)


def _platform() -> str:
    return sys.platform


def _answer(args: list[str]) -> proc.Result | None:
    """Run a trust-store tool; None when it is missing or cannot run."""
    if proc.which(args[0]) is None:
        return None
    try:
        return proc.run(args, check=False)
    except WtError:
        return None


def _ca_pem() -> str | None:
    try:
        root = caroot()
        return (root / "rootCA.pem").read_text().strip() if root else None
    except (WtError, OSError, UnicodeDecodeError):
        return None


def _in_p11_kit(_pem: str) -> bool | None:
    result = _answer(["trust", "list"])
    if result is None or not result.ok:
        return None
    labels = (line for line in result.stdout.splitlines() if line.strip().startswith("label:"))
    return any(_CA_LABEL in label for label in labels)


def _in_anchor_folders(pem: str) -> bool | None:
    checked = False
    for folder in _ANCHOR_DIRS:
        try:
            files = [path for path in folder.iterdir() if path.is_file()]
        except OSError:
            continue
        checked = True
        for path in files:
            try:
                if path.read_text().strip() == pem:
                    return True
            except (OSError, UnicodeDecodeError):
                continue
    return False if checked else None


def _in_chrome_nss_db(_pem: str) -> bool | None:
    try:
        database = Path.home() / ".pki" / "nssdb"
    except RuntimeError:
        return None
    result = _answer(["certutil", "-L", "-d", f"sql:{database}"])
    if result is None or not result.ok:  # a missing database is "not checked", not "not trusted"
        return None
    return _CA_LABEL in result.stdout


def _in_macos_keychains(_pem: str) -> bool | None:
    checked = False
    for keychain in _MACOS_KEYCHAINS:
        result = _answer(
            [
                "security",
                "find-certificate",
                "-a",
                "-c",
                _CA_LABEL,
                str(Path(keychain).expanduser()),
            ]
        )
        if result is None:
            return None
        checked = True
        if result.ok and result.stdout.strip():
            return True
    return False if checked else None


_STORE_CHECKS: dict[str, tuple[Callable[[str], bool | None], ...]] = {
    "linux": (_in_p11_kit, _in_anchor_folders, _in_chrome_nss_db),
    "darwin": (_in_macos_keychains,),
}


def ca_trusted() -> bool | None:
    """Is mkcert's root CA installed in a trust store? None: it cannot be told.

    True on any hit, False when at least one store was checked and none has it. Browsers show
    ERR_CERT_AUTHORITY_INVALID when the CA exists (`ca_ready`) but `mkcert -install` never ran.
    """
    pem = _ca_pem()
    checks = _STORE_CHECKS.get(_platform())
    if pem is None or checks is None:
        return None
    results = [check(pem) for check in checks]
    if True in results:
        return True
    return False if False in results else None


def install_ca() -> None:
    proc.run(["mkcert", "-install"], capture=False)


def cert_paths(names: Names) -> tuple[Path, Path]:
    return certs_dir() / f"{names.key}.pem", certs_dir() / f"{names.key}-key.pem"


def _hosts_file(cert: Path) -> Path:
    """Sidecar listing the names the certificate was made for."""
    return cert.with_suffix(".hosts")


def ensure_cert(names: Names) -> tuple[Path, Path] | None:
    """Create the branch certificate if mkcert is installed. None means: no local CA.

    An existing certificate is reused only while it was made for the current names, so a
    changed `domain` gets a new one.
    """
    cert, key = cert_paths(names)
    hosts = [names.branch_domain, f"*.{names.branch_domain}"]
    exists = cert.is_file() and key.is_file()
    sidecar = _hosts_file(cert)
    if exists and sidecar.is_file() and sidecar.read_text().split() == hosts:
        return cert, key
    if not available():
        return (cert, key) if exists else None
    certs_dir().mkdir(parents=True, exist_ok=True)
    proc.run(["mkcert", "-cert-file", str(cert), "-key-file", str(key), *hosts])
    sidecar.write_text("\n".join(hosts) + "\n")
    return cert, key


def remove_cert(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
        _hosts_file(path).unlink(missing_ok=True)
