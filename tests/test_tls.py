from pathlib import Path

import pytest

from wt import proc, tls
from wt.naming import Names

NAMES = Names(project="shop", branch="feat/cart", domain="shop.localhost")


def _stub_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> proc.Result:
        calls.append(list(args))
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


def test_ensure_cert_skips_mkcert_when_files_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    cert, key = tls.cert_paths(NAMES)
    cert.parent.mkdir(parents=True)
    cert.write_text("c")
    key.write_text("k")
    monkeypatch.setattr(proc, "which", lambda name: None)
    calls = _stub_run(monkeypatch)
    assert tls.ensure_cert(NAMES) == (cert, key)
    assert calls == []


def test_remove_cert_deletes_files_and_ignores_missing(tmp_path: Path) -> None:
    present = tmp_path / "a.pem"
    present.write_text("x")
    tls.remove_cert([present, tmp_path / "missing.pem"])
    assert not present.exists()
