from pathlib import Path

import pytest

from wt import proc, tls
from wt.naming import Names

NAMES = Names(project="shop", branch="feat/cart", domain="shop.localhost")


def _stub_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> proc.Result:
        calls.append(list(args))
        if args[0] == "mkcert":  # like mkcert: write the cert and key files
            Path(args[2]).write_text("c")
            Path(args[4]).write_text("k")
        return proc.Result(0, "", "")

    monkeypatch.setattr(proc, "run", fake_run)
    return calls


def test_ensure_cert_without_mkcert_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "which", lambda name: None)
    calls = _stub_run(monkeypatch)
    assert tls.ensure_cert(NAMES) is None
    assert calls == []


def test_ensure_cert_calls_mkcert_with_branch_and_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc, "which", lambda name: "/usr/bin/mkcert")
    calls = _stub_run(monkeypatch)
    cert, key = tls.cert_paths(NAMES)
    assert tls.ensure_cert(NAMES) == (cert, key)
    assert calls == [
        [
            "mkcert",
            "-cert-file",
            str(cert),
            "-key-file",
            str(key),
            NAMES.branch_domain,
            f"*.{NAMES.branch_domain}",
        ]
    ]
    assert NAMES.branch_domain.startswith(NAMES.slug)
    assert cert.parent.is_dir()


def _existing_cert() -> tuple[Path, Path]:
    cert, key = tls.cert_paths(NAMES)
    cert.parent.mkdir(parents=True)
    cert.write_text("c")
    key.write_text("k")
    return cert, key


def test_ensure_cert_reuses_cert_made_for_the_same_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "which", lambda name: "/usr/bin/mkcert")
    calls = _stub_run(monkeypatch)
    tls.ensure_cert(NAMES)
    assert tls.ensure_cert(NAMES) == tls.cert_paths(NAMES)
    assert len(calls) == 1


def test_ensure_cert_remakes_cert_after_domain_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "which", lambda name: "/usr/bin/mkcert")
    calls = _stub_run(monkeypatch)
    tls.ensure_cert(NAMES)
    moved = Names(project="shop", branch="feat/cart", domain="shop.test")
    assert tls.cert_paths(moved) == tls.cert_paths(NAMES)  # same files, new names
    tls.ensure_cert(moved)
    assert len(calls) == 2
    assert calls[1][-2:] == [moved.branch_domain, f"*.{moved.branch_domain}"]


def test_ensure_cert_remakes_cert_without_hosts_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    _existing_cert()  # made by an older wt: names unknown
    monkeypatch.setattr(proc, "which", lambda name: "/usr/bin/mkcert")
    calls = _stub_run(monkeypatch)
    tls.ensure_cert(NAMES)
    assert len(calls) == 1


def test_ensure_cert_keeps_existing_files_without_mkcert(monkeypatch: pytest.MonkeyPatch) -> None:
    cert, key = _existing_cert()
    monkeypatch.setattr(proc, "which", lambda name: None)
    calls = _stub_run(monkeypatch)
    assert tls.ensure_cert(NAMES) == (cert, key)
    assert calls == []


def test_remove_cert_deletes_files_and_ignores_missing(tmp_path: Path) -> None:
    present = tmp_path / "a.pem"
    present.write_text("x")
    sidecar = tmp_path / "a.hosts"
    sidecar.write_text("x")
    tls.remove_cert([present, tmp_path / "missing.pem"])
    assert not present.exists()
    assert not sidecar.exists()
