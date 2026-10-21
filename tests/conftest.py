import os
import subprocess
from pathlib import Path

import pytest
import yaml

from omniacli.auth import hash_password, hash_token
from omniacli.config import Restart, parse_settings

PASSWORD = "secret"
TOKEN = "mcp-test-token"
# linked instances: one may act on the servers, one only watches
LINK_TOKEN = "link-developer-token"
LINK_VIEW_TOKEN = "link-user-token"
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
    import omniacli.ops as ops

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
def fake_sudo(monkeypatch, tmp_path):
    """A sudo first in ops' PATH: it records its arguments, answers -l with
    yes, sets the VAR=value it is given and runs the command as this user."""
    bin_dir = tmp_path / "sudo-bin"
    bin_dir.mkdir()
    calls = tmp_path / "sudo.log"
    (bin_dir / "sudo").write_text(
        f'#!/bin/sh\necho "sudo $*" >> {calls}\n'
        'while [ $# -gt 0 ]; do case "$1" in\n'
        '  -n) shift ;;\n  -u) shift 2 ;;\n  -l) exit 0 ;;\n'
        '  *=*) export "$1"; shift ;;\n  *) break ;;\n'
        'esac; done\nexec "$@"\n')
    (bin_dir / "sudo").chmod(0o755)
    from omniacli import ops

    monkeypatch.setitem(ops._ENV, "PATH", f"{bin_dir}{os.pathsep}{ops._ENV.get('PATH', '')}")
    return calls


@pytest.fixture
def restart_calls(monkeypatch, tmp_path):
    calls = tmp_path / "calls.log"
    monkeypatch.setattr(
        Restart, "command",
        lambda self, action="restart": ["sh", "-c", f'echo "{action} {self.service}" >> {calls}']
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

    servers_file = tmp_path / "conf" / "servers.yaml"
    servers_file.parent.mkdir()
    servers_file.write_text(
        yaml.safe_dump(
            {"servers": {"srv": {"production": instance("production", "main"),
                                 "test": instance("test", "test")}}}
        )
    )
    settings = parse_settings(
        {
            "auth": {
                "users": {
                    "admin": USER_HASH,
                    "viewer": {"password": USER_HASH, "role": "user"},
                    "dev": {"password": USER_HASH, "role": "developer"},
                },
                "tokens": {"tester": hash_token(TOKEN)},
                "links": {
                    "omnia": {"token": hash_token(LINK_TOKEN), "role": "developer"},
                    "watcher": {"token": hash_token(LINK_VIEW_TOKEN), "role": "user"},
                },
            },
            "backup_dir": str(tmp_path / "backups"),
            "jobs_dir": str(tmp_path / "jobs"),
            "servers_file": "servers.yaml",
        },
        base_dir=servers_file.parent,
    )
    return {"settings": settings, "remotes": remotes, "tmp": tmp_path, "calls": restart_calls}
