import subprocess
from pathlib import Path

from wt.config import load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "notes"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def test_bootstrap_and_load_config(tmp_path: Path) -> None:
    target = tmp_path / "notes"
    subprocess.run([str(EXAMPLE / "bootstrap.sh"), str(target)], check=True, capture_output=True)

    back, front = target / "notes-back", target / "notes-front"
    assert "feat/tags" in git(back, "branch", "--list")
    assert git(back, "branch", "--show-current").strip() == "main"
    assert git(front, "branch", "--show-current").strip() == "main"
    assert not (tmp_path / "wt-home" / "catalog.yaml").exists()

    loaded = load_config(target / "worktree.yaml")
    cfg = loaded.config
    assert cfg.project == "notes"
    assert cfg.domain == "notes.localhost"
    assert loaded.uses == ("front",)  # the front consumes the back, so the back comes in too
    assert set(cfg.services) == {"back", "front"}
    assert loaded.remotes == {}
    assert cfg.services["front"].needs == ["back"]
    assert cfg.services["back"].compose_service == "api"
    assert cfg.services["front"].env["API_URL"] == "{{ services.back.url }}"
    assert cfg.services["back"].env["FRONT_ORIGIN"] == "{{ services.front.url }}"
    assert cfg.services["back"].env["DATABASE_URL"] == "{{ postgres.url('notes') }}"
    assert cfg.services["back"].env["BRANCH"] == "{{ branch }}"
    assert cfg.services["front"].env["BRANCH"] == "{{ branch }}"
    assert cfg.shared.postgres is not None
    assert cfg.shared.postgres.databases == ["notes"]
    assert loaded.repo_path(cfg.services["back"].repo) == back.resolve()
    assert loaded.repo_path(cfg.services["front"].repo) == front.resolve()


def test_bootstrap_refuses_non_empty_dir(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("x")
    result = subprocess.run([str(EXAMPLE / "bootstrap.sh"), str(tmp_path)], capture_output=True)
    assert result.returncode == 1
