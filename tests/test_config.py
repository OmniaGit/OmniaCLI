import datetime

import pytest

from omniaci.auth import Authenticator, hash_password, verify_password
from omniaci.config import (
    AutoUpdate,
    ConfigError,
    load_servers,
    parse_servers,
    parse_settings,
    save_servers,
)

T = datetime.time


def test_window_same_day():
    auto = AutoUpdate(window_start=T(8, 0), window_end=T(18, 0))
    assert auto.in_window(T(8, 0)) and auto.in_window(T(17, 59))
    assert not auto.in_window(T(18, 0)) and not auto.in_window(T(7, 59))


def test_window_across_midnight():
    auto = AutoUpdate(window_start=T(20, 0), window_end=T(6, 0))
    assert auto.in_window(T(23, 0)) and auto.in_window(T(2, 0))
    assert not auto.in_window(T(12, 0)) and not auto.in_window(T(6, 0))


def test_no_window_is_always():
    assert AutoUpdate().in_window(T(12, 0))


def _instance(repos):
    return {
        "database": "db",
        "odoo_config": "/c",
        "log_file": "/l",
        "venv_bin": "/b",
        "restart": {"type": "initd", "service": "odoo"},
        "repositories": [{"name": n, "path": f"/{n}", "branch": "x"} for n in repos],
        "auto_update": {"enabled": True, "window": "20:00-06:00"},
    }


def _settings(prod, test):
    return {"s": {"production": _instance(prod), "test": _instance(test)}}


def test_parse_ok():
    test = parse_servers(_settings(["odoo", "plm"], ["plm"]))["s"].test
    assert test.auto_update.window_label() == "20:00-06:00"
    assert test.restart.command() == ["sudo", "-n", "/etc/init.d/odoo", "restart"]
    test.restart.type = "systemd"
    assert test.restart.command() == ["sudo", "-n", "systemctl", "--no-ask-password", "restart", "odoo"]


def test_test_repository_needs_production_pair():
    with pytest.raises(ConfigError, match="plm"):
        parse_servers(_settings(["odoo"], ["odoo", "plm"]))


def test_bad_restart_type():
    data = _settings(["odoo"], ["odoo"])
    data["s"]["test"]["restart"]["type"] = "docker"
    with pytest.raises(ConfigError, match="restart"):
        parse_servers(data)


def test_password_hash():
    encoded = hash_password("pw", iterations=1000)
    assert verify_password("pw", encoded)
    assert not verify_password("other", encoded)
    assert not verify_password("pw", "garbage")


def test_authenticator():
    auth = Authenticator({"u": hash_password("pw", iterations=1000)})
    assert auth.check("u", "pw") and auth.check("u", "pw")  # second from cache
    assert not auth.check("u", "wrong")
    assert not auth.check("nobody", "pw")


def test_bad_server_name():
    with pytest.raises(ConfigError, match="name"):
        parse_servers({"a/b": {"test": _instance(["odoo"])}})


def test_servers_file_round_trip(tmp_path):
    data = _settings(["odoo", "plm"], ["plm"])
    data["s"]["production"]["odoo_bin"] = "/opt/odoo/odoo-bin"
    servers = parse_servers(data)
    assert str(servers["s"].production.odoo_bin) == "/opt/odoo/odoo-bin"
    assert servers["s"].test.odoo_bin is None
    path = tmp_path / "servers.yaml"
    save_servers(path, servers)
    assert load_servers(path) == servers
    save_servers(path, {})
    assert load_servers(path) == {}
    assert load_servers(path.with_name("servers.yaml.bak")) == servers


def test_settings_servers_file_relative(tmp_path):
    main = {"auth": {"users": {"a": "h"}}, "backup_dir": "/b", "jobs_dir": "/j"}
    settings = parse_settings(main, base_dir=tmp_path)
    assert settings.servers_file == tmp_path / "servers.yaml" and settings.servers == {}
