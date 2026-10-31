import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from wt import compose, host, proc
from wt.errors import WtError


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: ("RESET", node.value or None))


def test_override_resets_ports_and_joins_network() -> None:
    doc = compose.build_override(
        ["api", "db"],
        {"api": compose.ServicePatch(alias="wt-shop-x-api", env={"A": "1"})},
    )
    text = compose.dump(doc)
    assert "ports: !reset []" in text
    assert "container_name: !reset ''" in text
    parsed = yaml.load(text, Loader=_Loader)
    api = parsed["services"]["api"]
    assert api["environment"] == {"A": "1"}
    assert api["networks"]["wt"]["aliases"] == ["wt-shop-x-api"]
    assert "default" in api["networks"]
    assert "networks" not in parsed["services"]["db"]
    assert parsed["networks"]["wt"] == {"external": True, "name": "wt"}


def test_override_without_routes_has_no_network() -> None:
    doc = compose.build_override(["worker"], {"worker": compose.ServicePatch(env={"A": "1"})})
    assert "networks" not in doc


@pytest.mark.parametrize("as_array", [True, False])
def test_ps_parses_both_json_formats(monkeypatch: pytest.MonkeyPatch, as_array: bool) -> None:
    rows = [
        {"Service": "api", "State": "running", "Health": "healthy"},
        {"Service": "db", "State": "exited", "Health": ""},
    ]
    out = json.dumps(rows) if as_array else "\n".join(json.dumps(r) for r in rows)
    monkeypatch.setattr(proc, "run", lambda *a, **k: proc.Result(0, out, ""))
    assert compose.ps("p") == [
        compose.Container("api", "running", "healthy"),
        compose.Container("db", "exited", ""),
    ]


def test_ps_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "run", lambda *a, **k: proc.Result(0, "", ""))
    assert compose.ps("p") == []


def test_ps_raises_when_docker_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "run", lambda *a, **k: proc.Result(1, "", "Cannot connect"))
    with pytest.raises(WtError, match="docker is not reachable"):
        compose.ps("p")


def _record(
    monkeypatch: pytest.MonkeyPatch, stdout: str = ""
) -> list[tuple[list[str], dict[str, object]]]:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake(args: list[str], **kwargs: object) -> proc.Result:
        calls.append((list(args), kwargs))
        return proc.Result(0, stdout, "")

    monkeypatch.setattr(proc, "run", fake)
    return calls


@pytest.mark.parametrize("build", [True, False])
def test_up_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, build: bool) -> None:
    calls = _record(monkeypatch)
    user, override = tmp_path / "compose.yaml", tmp_path / "override.yaml"
    compose.up("proj", [user, override], cwd=tmp_path, build=build, timeout=90)
    args, kwargs = calls[0]
    assert args[:4] == ["docker", "compose", "-p", "proj"]
    assert args[4:8] == ["-f", str(user), "-f", str(override)]
    assert "--wait-timeout" in args and args[args.index("--wait-timeout") + 1] == "90"
    assert ("--build" in args) is build
    assert kwargs["cwd"] == tmp_path


def test_up_pulls_quietly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _record(monkeypatch)
    compose.up("proj", [tmp_path / "c.yaml"], cwd=tmp_path, build=False, timeout=90)
    assert "--quiet-pull" in calls[0][0]


def test_down_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)
    compose.down("proj")
    assert calls[0][0] == [
        "docker", "compose", "-p", "proj", "down", "-v", "--remove-orphans",
    ]  # fmt: skip


def test_list_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _record(monkeypatch, "api\n db \n\n")
    f = tmp_path / "compose.yaml"
    assert compose.list_services(f) == ["api", "db"]
    assert calls[0][0][-2:] == ["config", "--services"]
    assert ["-f", str(f)] == calls[0][0][4:6]
    assert calls[0][1]["cwd"] == tmp_path


def _network_fake(results: list[int]) -> tuple[list[list[str]], object]:
    calls: list[list[str]] = []
    codes = iter(results)

    def fake(args: list[str], **kwargs: object) -> proc.Result:
        calls.append(list(args))
        res = proc.Result(next(codes), "", "already exists")
        if args[2] == "create" and res.returncode != 0 and kwargs.get("check", True):
            raise proc.CommandError(args, res)
        return res

    return calls, fake


def test_ensure_network_race(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, fake = _network_fake([1, 1, 0])
    monkeypatch.setattr(proc, "run", fake)
    compose.ensure_network()
    assert [c[2] for c in calls] == ["inspect", "create", "inspect"]


def test_ensure_network_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = _network_fake([1, 1, 1])
    monkeypatch.setattr(proc, "run", fake)
    with pytest.raises(proc.CommandError):
        compose.ensure_network()


def test_override_network_without_alias_joins_the_shared_network() -> None:
    doc = compose.build_override(["db"], {"db": compose.ServicePatch(network=True)})
    assert doc["services"]["db"]["networks"] == {"default": {}, "wt": {}}
    assert doc["networks"]["wt"] == {"external": True, "name": "wt"}


def test_env_dollar_is_escaped_for_compose() -> None:
    env = {"A": "ab$cd", "HASH": "$2b$12$abcdefghij", "PLAIN": "x"}
    doc = compose.build_override(["api"], {"api": compose.ServicePatch(env=env)})
    assert doc["services"]["api"]["environment"] == {
        "A": "ab$$cd",
        "HASH": "$$2b$$12$$abcdefghij",
        "PLAIN": "x",
    }
    assert env["A"] == "ab$cd"  # the patch itself is not mutated


def test_up_passes_on_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _record(monkeypatch)
    sink: Callable[[str], None] = print
    compose.up("p", [tmp_path / "c.yaml"], cwd=tmp_path, build=False, timeout=9, on_line=sink)
    assert calls[0][1]["on_line"] is sink and calls[0][1]["capture"] is False


def test_up_without_on_line_prints_to_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record(monkeypatch)
    compose.up("p", [tmp_path / "c.yaml"], cwd=tmp_path, build=False, timeout=9)
    assert calls[0][1].get("on_line") is None


def test_override_publishes_host_port_on_loopback() -> None:
    doc = compose.build_override(
        ["api", "db"], {"api": compose.ServicePatch(publish=(10101, 8080))}
    )
    text = compose.dump(doc)
    assert "ports: !override\n    - 127.0.0.1:10101:8080" in text
    assert "ports: !reset []" in text  # db keeps today's behavior


def _docker_port(monkeypatch: pytest.MonkeyPatch, *outputs: tuple[int, str]) -> list[list[str]]:
    """Fake `docker port`: each call returns the next (returncode, stdout); the last repeats."""
    calls: list[list[str]] = []
    pending = list(outputs)

    def run(args: list[str], **kw: object) -> proc.Result:
        calls.append(list(args))
        code, out = pending.pop(0) if len(pending) > 1 else pending[0]
        return proc.Result(code, out, "")

    monkeypatch.setattr(proc, "run", run)
    return calls


def test_published_host_ports_parses_ipv4_ipv6_and_many_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = "80/tcp -> 0.0.0.0:80\n443/tcp -> [::]:443\n5432/tcp -> 127.0.0.1:55432\n\n"
    calls = _docker_port(monkeypatch, (0, out))
    assert compose.published_host_ports("wt-traefik") == {80, 443, 55432}
    assert calls == [["docker", "port", "wt-traefik"]]


def test_published_host_ports_ignores_lines_it_cannot_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker_port(monkeypatch, (0, "garbage\n5432/tcp -> 127.0.0.1:x\n6379/tcp -> 127.0.0.1:6380\n"))
    assert compose.published_host_ports("c") == {6380}


@pytest.mark.parametrize("result", [(0, ""), (1, "Error: No such container: c\n")])
def test_published_host_ports_empty_for_stopped_or_missing(
    monkeypatch: pytest.MonkeyPatch, result: tuple[int, str]
) -> None:
    _docker_port(monkeypatch, result)
    assert compose.published_host_ports("c") == set()


def test_ensure_published_does_nothing_when_all_published(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker_port(monkeypatch, (0, "5432/tcp -> 127.0.0.1:55432\n"))
    recreated: list[bool] = []
    compose.ensure_published("c", [55432], lambda: recreated.append(True))
    assert recreated == []


def test_ensure_published_with_no_ports_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _docker_port(monkeypatch, (0, ""))
    compose.ensure_published("c", [], lambda: pytest.fail("must not recreate"))
    assert calls == []


def test_ensure_published_recreates_once_then_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker_port(monkeypatch, (0, ""), (0, "5432/tcp -> 127.0.0.1:55432\n"))
    recreated: list[bool] = []
    compose.ensure_published("c", [55432], lambda: recreated.append(True))
    assert recreated == [True]


def test_ensure_published_names_the_busy_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker_port(monkeypatch, (0, "1/tcp -> 127.0.0.1:1\n"))
    monkeypatch.setattr(host, "port_free", lambda port: False)
    recreated: list[bool] = []
    with pytest.raises(WtError, match=r"^port 7 is already in use by another program$"):
        compose.ensure_published("c", [9, 1, 7], lambda: recreated.append(True))
    assert recreated == [True]


def test_ensure_published_names_the_unpublished_port_when_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _docker_port(monkeypatch, (1, "Error: No such container: c\n"))
    monkeypatch.setattr(host, "port_free", lambda port: True)
    with pytest.raises(WtError, match=r"^c does not publish port 7$"):
        compose.ensure_published("c", [9, 7], lambda: None)


def _failing_recreate() -> None:
    raise WtError("docker: bind: address already in use")


def test_ensure_published_names_the_busy_port_when_the_recreate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _docker_port(monkeypatch, (0, "1/tcp -> 127.0.0.1:1\n"))
    probed: list[int] = []

    def port_free(port: int) -> bool:
        probed.append(port)
        return port != 7

    monkeypatch.setattr(host, "port_free", port_free)
    with pytest.raises(WtError, match=r"^port 7 is already in use by another program$") as caught:
        compose.ensure_published("c", [9, 1, 7], _failing_recreate)
    assert "address already in use" in str(caught.value.__cause__)
    assert probed == [7]


def test_ensure_published_keeps_the_original_error_when_ports_are_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _docker_port(monkeypatch, (0, ""))
    monkeypatch.setattr(host, "port_free", lambda port: True)
    with pytest.raises(WtError, match=r"^docker: bind: address already in use$") as caught:
        compose.ensure_published("c", [9, 7], _failing_recreate)
    assert caught.value.__cause__ is None
