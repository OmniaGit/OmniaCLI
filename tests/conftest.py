import subprocess
from pathlib import Path

import pytest

from omniaci.auth import hash_password
from omniaci.config import Restart, parse_settings

PASSWORD = "secret"
USER_HASH = hash_password(PASSWORD, iterations=1000)


def sh(*cmd, cwd=None) -> str:
    return subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    sh("git", "add", name, cwd=repo)
    sh("git", "commit", "-q", "-m", message, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


def make_remote(base: Path, name: str) -> Path:
    """A bare remote with branch main and a test branch forked from it."""
    remote = base / f"{name}.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(remote))
    seed = base / f"{name}_seed"
    sh("git", "clone", "-q", str(remote), str(seed))
    commit(seed, "base.txt", "base\n", "initial")
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:test", cwd=seed)
    return remote


def clone(remote: Path, dest: Path, branch: str) -> Path:
    sh("git", "clone", "-q", "-b", branch, str(remote), str(dest))
    return dest


@pytest.fixture(autouse=True)
def git_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    # ops copies os.environ at import time
    import omniaci.ops as ops

    monkeypatch.setattr(ops, "_ENV", {**ops._ENV, **{
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }})


@pytest.fixture
def fake_venv(tmp_path) -> Path:
    """click-odoo scripts that record their calls instead of touching Odoo."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "calls.log"
    (bin_dir / "click-odoo-update").write_text(
        f'#!/bin/sh\necho "update $*" >> {calls}\necho updated\n'
    )
    (bin_dir / "click-odoo-backupdb").write_text(
        f'#!/bin/sh\necho "backup $*" >> {calls}\n'
        'for last; do :; done\necho dump > "$last"\n'
    )
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    return bin_dir


@pytest.fixture
def restart_calls(monkeypatch, tmp_path):
    calls = tmp_path / "calls.log"
    monkeypatch.setattr(
        Restart, "command", lambda self: ["sh", "-c", f'echo "restart {self.service}" >> {calls}']
    )
    return calls


@pytest.fixture
def env(tmp_path, fake_venv, restart_calls):
    """A server with production on main and test on test, two repositories."""
    remotes = {name: make_remote(tmp_path, name) for name in ("odoo", "addons")}
    log = tmp_path / "odoo.log"
    log.write_text("".join(f"line {i}\n" for i in range(10)))

    def instance(role, branch):
        return {
            "database": f"DB_{role}",
            "odoo_config": str(tmp_path / "odoo.conf"),
            "log_file": str(log),
            "venv_bin": str(fake_venv),
            "restart": {"type": "systemd", "service": f"odoo-{role}", "sudo": False},
            "repositories": [
                {
                    "name": name,
                    "path": str(clone(remote, tmp_path / role / name, branch)),
                    "branch": branch,
                }
                for name, remote in remotes.items()
            ],
        }

    settings = parse_settings(
        {
            "auth": {"users": {"admin": USER_HASH}},
            "backup_dir": str(tmp_path / "backups"),
            "jobs_dir": str(tmp_path / "jobs"),
            "servers": {
                "srv": {"production": instance("production", "main"), "test": instance("test", "test")}
            },
        }
    )
    return {"settings": settings, "remotes": remotes, "tmp": tmp_path, "calls": restart_calls}
