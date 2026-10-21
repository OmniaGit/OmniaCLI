import datetime
from pathlib import Path

import pytest

from omniacli.auth import Authenticator, hash_password, verify_password
from omniacli.config import (
    AutoUpdate,
    ConfigError,
    load_servers,
    load_settings,
    parse_servers,
    parse_auth,
    parse_settings,
    save_servers,
    server_to_dict,
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


def test_http_port():
    data = _settings(["odoo"], ["odoo"])
    data["s"]["test"]["http_port"] = 8123
    with pytest.raises(ConfigError, match="only with restart type process"):
        parse_servers(data)
    data["s"]["test"]["restart"] = {"type": "process"}
    servers = parse_servers(data, Path("/logs"))
    assert servers["s"].test.http_port == 8123 and servers["s"].production.http_port is None
    saved = server_to_dict(servers["s"])
    assert saved["test"]["http_port"] == 8123 and "http_port" not in saved["production"]
    for bad in (0.5, "80a", 70000, True):
        data["s"]["test"]["http_port"] = bad
        with pytest.raises(ConfigError, match="not a port"):
            parse_servers(data, Path("/logs"))


def test_test_repository_needs_production_pair():
    with pytest.raises(ConfigError, match="plm"):
        parse_servers(_settings(["odoo"], ["odoo", "plm"]))


def test_run_as():
    data = _settings(["odoo"], ["odoo"])
    data["s"]["test"]["run_as"] = "odoo"
    servers = parse_servers(data)
    assert servers["s"].test.run_as == "odoo" and servers["s"].production.run_as is None
    saved = server_to_dict(servers["s"])
    assert saved["test"]["run_as"] == "odoo" and "run_as" not in saved["production"]
    data["s"]["test"]["run_as"] = "odoo; rm"
    with pytest.raises(ConfigError, match="not a user name"):
        parse_servers(data)
    data["s"]["test"]["run_as"] = "odoo"
    data["s"]["test"]["restart"] = {"type": "process"}
    with pytest.raises(ConfigError, match="not available with restart type process"):
        parse_servers(data, Path("/logs"))


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


def test_settings_file_missing(tmp_path):
    with pytest.raises(ConfigError, match="cannot read .*omniacli.yaml: No such file"):
        load_settings(tmp_path / "omniacli.yaml")


def test_incomplete_draft_kept_active_refused(tmp_path):
    data = _settings(["odoo"], ["odoo"])
    del data["s"]["test"]["repositories"][0]["path"]
    with pytest.raises(ConfigError, match="path"):
        parse_servers(data)
    data["s"]["status"] = "draft"
    draft = parse_servers(data)["s"]
    assert not draft.active and draft.instances == {} and "missing 'path'" in draft.error
    path = tmp_path / "servers.yaml"
    save_servers(path, {"s": draft})
    assert load_servers(path) == {"s": draft}


def test_auth_roles_and_links():
    access = parse_auth({
        "users": {"old": "pbkdf2_sha256$1$aa$bb", "dev": {"password": "h", "role": "developer"}},
        "links": {"omnia": {"token": "sha256$" + "0" * 64, "role": "user"}},
    })
    assert access.users == {"old": "pbkdf2_sha256$1$aa$bb", "dev": "h"}
    # a user in the format before roles is an admin
    assert access.roles == {"old": "admin", "dev": "developer"} and access.tokens == {}
    link = access.links["omnia"]
    assert link.role == "user" and link.token == "sha256$" + "0" * 64


@pytest.mark.parametrize("auth, message", [
    ({"users": {"x": {"password": "h", "role": "boss"}}}, "auth.users.x: role must be one of"),
    ({"users": {"x": {"role": "user"}}}, "auth.users.x: password hash missing"),
    ({"users": {}}, "auth.users: at least one user is required"),
    ({"users": {"x": "h"}, "links": {"c": {"token": "plain", "role": "user"}}}, "auth.links.c: token is not"),
    ({"users": {"x": "h"}, "links": {"c": {"token": "sha256$" + "0" * 64}}}, "auth.links.c: role must be"),
])
def test_auth_errors(auth, message):
    with pytest.raises(ConfigError, match=message):
        parse_auth(auth)


def test_users_file_round_trip_and_main_config_wins(tmp_path):
    from omniacli.config import Access, LinkAccess, load_access, save_access

    path = tmp_path / "users.yaml"
    assert load_access(path) == Access()
    web = Access({"anna": "h1", "admin": "web-hash"}, {"anna": "developer", "admin": "user"},
                 {"laptop": "sha256$" + "1" * 64}, {"partner": LinkAccess("sha256$" + "2" * 64, "user")},
                 {"laptop": "admin"})
    save_access(path, web)
    assert load_access(path) == web and path.stat().st_mode & 0o777 == 0o600
    main = {"auth": {"users": {"admin": "main-hash"}}, "backup_dir": "/b", "jobs_dir": str(tmp_path / "jobs"),
            "servers_file": str(tmp_path / "servers.yaml")}
    settings = parse_settings(main)
    # the users file is next to the servers file; the main config wins a clash
    assert settings.users_file == path
    assert settings.users == {"anna": "h1", "admin": "main-hash"}
    assert settings.user_role("admin") == "admin" and settings.user_role("anna") == "developer"
    assert settings.tokens == {"laptop": "sha256$" + "1" * 64} and settings.links["partner"].role == "user"
    assert settings.token_role("laptop") == "admin"
    # a token saved before roles: developer, what it could do then
    path.write_text("tokens:\n  old: sha256$" + "3" * 64 + "\n")
    assert load_access(path).token_roles == {"old": "developer"}


def test_groups_parse_match_and_round_trip(tmp_path):
    from omniacli.config import ServerGroup, load_groups, parse_groups, save_groups

    groups = parse_groups([{"name": "Customers", "members": ["cliente/*", "acme/one", "acme/one", " "]},
                           {"name": "Mine", "members": ["*"]}])
    assert groups[0].members == ["cliente/*", "acme/one"]  # once each, blanks dropped
    assert groups[0].contains("cliente", "any") and groups[0].contains("acme", "one")
    assert not groups[0].contains("acme", "two") and not groups[0].contains("", "cliente")
    assert groups[1].contains("", "srv") and not groups[1].contains("cliente", "srv")
    path = tmp_path / "groups.yaml"
    assert load_groups(path) == []
    save_groups(path, groups)
    assert load_groups(path) == groups
    for data, error in (([{"name": ""}], "needs a name"), ([{"name": "a"}, {"name": "a"}], "two groups"),
                        ([{"name": "a", "members": ["a b"]}], "is not a server"),
                        ([{"name": "a", "members": ["x/y/z"]}], "is not a server"), ({"a": 1}, "must be a list")):
        with pytest.raises(ConfigError, match=error):
            parse_groups(data)
    assert ServerGroup("x", ["*/db"]).contains("anylink", "db")
