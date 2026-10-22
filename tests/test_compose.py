import json

import pytest
import yaml

from wt import compose, proc


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
