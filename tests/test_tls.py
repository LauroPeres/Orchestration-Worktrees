from pathlib import Path

import pytest

from wt import proc, tls
from wt.errors import WtError
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


def _fake_caroot(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(proc, "which", lambda name: "/usr/bin/mkcert")
    monkeypatch.setattr(proc, "run", lambda args, **kwargs: proc.Result(0, f"{root}\n", ""))


def test_caroot_returns_the_mkcert_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_caroot(monkeypatch, tmp_path)
    assert tls.caroot() == tmp_path


def test_caroot_is_none_without_mkcert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "which", lambda name: None)
    assert tls.caroot() is None


def test_ca_ready_needs_the_root_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_caroot(monkeypatch, tmp_path)
    assert tls.ca_ready() is False
    (tmp_path / "rootCA.pem").write_text("pem")
    assert tls.ca_ready() is True


CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIBmkcert\n-----END CERTIFICATE-----\n"
TRUST_WITH_CA = "pkcs11:id=%01;type=cert\n    type: certificate\n    label: mkcert dev@box\n"
TRUST_WITHOUT_CA = "pkcs11:id=%02;type=cert\n    type: certificate\n    label: ISRG Root X1\n"


class _Stores:
    """A fake machine: what `trust`, `certutil` and `security` answer, and which tools exist."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.root = tmp_path / "caroot"
        self.root.mkdir()
        (self.root / "rootCA.pem").write_text(CA_PEM)
        self.anchors = tmp_path / "anchors"
        self.tools = {"mkcert", "trust", "certutil"}
        self.answers: dict[str, proc.Result | None] = {}  # None: the command cannot run
        self.listings: dict[str, proc.Result] = {}  # `certutil -L` answer per NSS folder
        self.calls: list[list[str]] = []
        monkeypatch.setattr(tls, "_platform", lambda: "linux")
        monkeypatch.setattr(tls, "_ANCHOR_DIRS", (self.anchors,))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr(
            proc, "which", lambda name: f"/bin/{name}" if name in self.tools else None
        )
        monkeypatch.setattr(proc, "run", self._run)

    def _run(self, args: list[str], **kwargs: object) -> proc.Result:
        self.calls.append(list(args))
        if args[0] == "mkcert":
            return proc.Result(0, f"{self.root}\n", "")
        if args[:2] == ["certutil", "-L"] and args[-1].removeprefix("sql:") in self.listings:
            return self.listings[args[-1].removeprefix("sql:")]
        answer = self.answers.get(args[0], proc.Result(0, "", ""))
        if answer is None:
            raise WtError(f"could not run {args[0]}")
        return answer

    def put_nss(self, folder: Path, listing: str = "") -> Path:
        """An NSS database that lists `listing` (what `certutil -L` prints)."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "cert9.db").write_text("db")
        self.listings[str(folder)] = proc.Result(0, listing, "")
        return folder

    def installs(self) -> list[list[str]]:
        return [call for call in self.calls if call[:1] == ["certutil"] and "-A" in call]

    def put_anchor(self, content: str) -> None:
        self.anchors.mkdir(exist_ok=True)
        (self.anchors / "mkcert.crt").write_text(content)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Stores:
    return _Stores(monkeypatch, tmp_path)


def test_ca_trusted_hit_in_p11_kit_trust_list(stores: _Stores) -> None:
    stores.answers["trust"] = proc.Result(0, TRUST_WITH_CA, "")
    assert tls.ca_trusted() is True
    assert ["trust", "list"] in stores.calls


def test_ca_trusted_hit_in_an_anchor_file(stores: _Stores) -> None:
    stores.answers["trust"] = proc.Result(0, TRUST_WITHOUT_CA, "")
    stores.put_anchor(CA_PEM)
    assert tls.ca_trusted() is True


def test_ca_trusted_ignores_an_anchor_with_other_content(stores: _Stores) -> None:
    stores.tools = {"mkcert"}
    stores.put_anchor("-----BEGIN CERTIFICATE-----\nother\n-----END CERTIFICATE-----\n")
    assert tls.ca_trusted() is False


def test_ca_trusted_hit_in_the_legacy_nss_database(stores: _Stores, home: Path) -> None:
    stores.answers["trust"] = proc.Result(0, TRUST_WITHOUT_CA, "")
    database = stores.put_nss(home / ".pki" / "nssdb", "mkcert dev@box     C,,\n")
    assert tls.ca_trusted() is True
    assert ["certutil", "-L", "-d", f"sql:{database}"] in stores.calls


def test_ca_trusted_hit_in_the_xdg_nss_database(stores: _Stores, home: Path) -> None:
    stores.answers["trust"] = proc.Result(0, TRUST_WITHOUT_CA, "")
    stores.put_nss(home / ".pki" / "nssdb", "Digicert   C,,\n")
    stores.put_nss(home / ".local" / "share" / "pki" / "nssdb", "mkcert dev@box     C,,\n")
    assert tls.ca_trusted() is True


def test_ca_trusted_follows_xdg_data_home(
    stores: _Stores, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stores.tools = {"mkcert", "certutil"}
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    stores.put_nss(data_home / "pki" / "nssdb", "mkcert dev@box     C,,\n")
    assert tls.ca_trusted() is True


def test_ca_trusted_nss_database_without_the_ca_is_a_checked_miss(
    stores: _Stores, home: Path
) -> None:
    stores.tools = {"mkcert", "certutil"}
    stores.put_nss(home / ".local" / "share" / "pki" / "nssdb", "Digicert   C,,\n")
    assert tls.ca_trusted() is False


def test_ca_trusted_nss_database_that_cannot_be_read_is_unchecked(
    stores: _Stores, home: Path
) -> None:
    stores.tools = {"mkcert", "certutil"}
    stores.put_nss(home / ".pki" / "nssdb")
    stores.listings.clear()
    stores.answers["certutil"] = proc.Result(255, "", "could not open the database")
    assert tls.ca_trusted() is None


def test_ca_trusted_is_false_when_every_checkable_store_misses(stores: _Stores, home: Path) -> None:
    stores.answers["trust"] = proc.Result(0, TRUST_WITHOUT_CA, "")
    stores.put_nss(home / ".pki" / "nssdb", "Digicert   C,,\n")
    stores.put_anchor("something else")
    assert tls.ca_trusted() is False


def test_ca_trusted_is_false_when_only_trust_list_could_be_checked(stores: _Stores) -> None:
    stores.tools = {"mkcert", "trust"}
    stores.answers["trust"] = proc.Result(0, TRUST_WITHOUT_CA, "")
    assert tls.ca_trusted() is False


def test_ca_trusted_is_none_when_no_store_can_be_checked(stores: _Stores) -> None:
    stores.tools = {"mkcert"}  # no trust, no certutil, and the anchor folder does not exist
    assert tls.ca_trusted() is None


def test_ca_trusted_treats_a_failing_trust_command_as_unchecked(stores: _Stores) -> None:
    stores.tools = {"mkcert", "trust"}
    stores.answers["trust"] = proc.Result(1, "", "boom")
    assert tls.ca_trusted() is None


def test_ca_trusted_treats_a_trust_command_that_cannot_run_as_unchecked(
    stores: _Stores,
) -> None:
    stores.tools = {"mkcert", "trust"}
    stores.answers["trust"] = None
    assert tls.ca_trusted() is None


def test_ca_trusted_certutil_without_a_database_is_unchecked(stores: _Stores) -> None:
    stores.tools = {"mkcert", "certutil"}
    assert tls.ca_trusted() is None
    assert [call for call in stores.calls if call[0] == "certutil"] == []


def test_ca_trusted_is_none_without_the_ca_file(stores: _Stores) -> None:
    (stores.root / "rootCA.pem").unlink()
    assert tls.ca_trusted() is None


def test_ca_trusted_is_none_without_mkcert(stores: _Stores) -> None:
    stores.tools = set()
    assert tls.ca_trusted() is None


def test_ca_trusted_is_none_on_an_unknown_platform(
    stores: _Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tls, "_platform", lambda: "win32")
    assert tls.ca_trusted() is None
    assert [call for call in stores.calls if call[0] != "mkcert"] == []


def test_ca_trusted_on_macos_finds_the_ca_in_a_keychain(
    stores: _Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tls, "_platform", lambda: "darwin")
    stores.tools = {"mkcert", "security"}
    stores.answers["security"] = proc.Result(
        0, 'keychain: "/Library/Keychains/System.keychain"', ""
    )
    assert tls.ca_trusted() is True
    assert stores.calls[-1][:4] == ["security", "find-certificate", "-a", "-c"]


def test_ca_trusted_on_macos_is_false_when_no_keychain_has_it(
    stores: _Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tls, "_platform", lambda: "darwin")
    stores.tools = {"mkcert", "security"}
    stores.answers["security"] = proc.Result(44, "", "could not be found")
    assert tls.ca_trusted() is False


def test_install_ca_user_trusts_the_ca_in_both_nss_databases(stores: _Stores, home: Path) -> None:
    legacy = stores.put_nss(home / ".pki" / "nssdb")
    xdg = stores.put_nss(home / ".local" / "share" / "pki" / "nssdb")
    assert tls.install_ca_user() == [legacy, xdg]
    assert stores.installs() == [
        [
            "certutil",
            "-d",
            f"sql:{folder}",
            "-A",
            "-t",
            "C,,",
            "-n",
            "mkcert development CA (wt)",
            "-i",
            str(stores.root / "rootCA.pem"),
        ]
        for folder in (legacy, xdg)
    ]


def test_install_ca_user_follows_xdg_data_home(
    stores: _Stores, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    elsewhere = stores.put_nss(data_home / "pki" / "nssdb")
    stores.put_nss(tmp_path / "home" / ".local" / "share" / "pki" / "nssdb")  # not the XDG one now
    assert tls.install_ca_user() == [elsewhere]


def test_install_ca_user_skips_a_database_that_already_lists_the_ca(
    stores: _Stores, home: Path
) -> None:
    stores.put_nss(home / ".pki" / "nssdb", "mkcert development CA (wt)   C,,\n")
    fresh = stores.put_nss(home / ".local" / "share" / "pki" / "nssdb", "Digicert   C,,\n")
    assert tls.install_ca_user() == [fresh]
    assert len(stores.installs()) == 1


def test_install_ca_user_does_nothing_when_already_installed_everywhere(
    stores: _Stores, home: Path
) -> None:
    stores.put_nss(home / ".pki" / "nssdb", "mkcert dev@box   C,,\n")
    assert tls.install_ca_user() == []
    assert stores.installs() == []


def test_install_ca_user_reports_only_the_databases_where_certutil_succeeded(
    stores: _Stores, home: Path
) -> None:
    stores.put_nss(home / ".pki" / "nssdb")
    stores.answers["certutil"] = proc.Result(255, "", "read-only database")
    assert tls.install_ca_user() == []
    assert len(stores.installs()) == 1


def test_install_ca_user_never_raises_when_certutil_cannot_run(stores: _Stores, home: Path) -> None:
    stores.put_nss(home / ".pki" / "nssdb")
    stores.answers["certutil"] = None
    assert tls.install_ca_user() == []


def test_install_ca_user_ignores_missing_databases(stores: _Stores, home: Path) -> None:
    (home / ".pki" / "nssdb").mkdir(parents=True)  # a folder without cert9.db is not a database
    assert tls.install_ca_user() == []
    assert stores.installs() == []


def test_install_ca_user_needs_certutil(stores: _Stores, home: Path) -> None:
    stores.put_nss(home / ".pki" / "nssdb")
    stores.tools = {"mkcert"}
    assert tls.install_ca_user() == []


def test_install_ca_user_needs_the_ca_file(stores: _Stores, home: Path) -> None:
    stores.put_nss(home / ".pki" / "nssdb")
    (stores.root / "rootCA.pem").unlink()
    assert tls.install_ca_user() == []
    assert stores.installs() == []
