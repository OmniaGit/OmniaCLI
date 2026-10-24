# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scheduled actions: the file, the page, a run of each step (the copy
through the link of test_from_link's here, in the same process)."""

import datetime
import json
import os
import sys
import time

import pytest
import yaml

from omniacli import schedules
from omniacli.config import ConfigError
from omniacli.jobs import Job

from .test_from_link import calls, here, wait  # noqa: F401 - fixture
from .test_links import as_user


def a_schedule(**extra):
    return {"instance": "srv/test", "cron": "0 2 * * *", **extra}


def test_parse_and_back():
    s = schedules.parse_schedule("nightly", a_schedule(
        pull=True, user="matteo",
        copy={"link": "cliente", "server": "srv", "keep_backups": 2, "restore_options": {"neutralize": True}},
        script={"name": "fix.py", "args": "--db 'a b'"}))
    assert s.acting_user == "matteo" and s.copy.source.role == "production" and s.copy.keep_backups == 2
    assert s.steps == ["git pull", "copy from cliente › srv/production", "script fix.py"]
    assert schedules.parse_schedule("nightly", schedules.schedule_to_dict(s)) == s
    assert schedules.parse_schedule("n", a_schedule(pull=True)).acting_user == "omniacli"
    assert schedules.parse_schedule("n", a_schedule(pull=True)).history == 30


@pytest.mark.parametrize("data, message", [
    (a_schedule(), "at least one step"),
    (a_schedule(pull=True, cron="0 2 * *"), "5 fields"),
    (a_schedule(pull=True, cron="0 0 2 * * *"), "5 fields"),
    (a_schedule(pull=True, cron="61 * * * *"), "5 fields"),
    (a_schedule(pull=True, instance="srv"), "server/role"),
    (a_schedule(pull=True, user="a b"), "not a user name"),
    (a_schedule(script={"name": "../x.py"}), "a .py file"),
    (a_schedule(script={"name": "x.py", "args": "'open"}), "script.args"),
    (a_schedule(pull=True, history=0), "at least 1"),
    (a_schedule(copy={"link": "cliente", "server": "srv", "keep_backups": 0}), "at least 1"),
])
def test_refused(data, message):
    with pytest.raises(ConfigError, match=message):
        schedules.parse_schedule("n", data)


def test_next_runs():
    runs = schedules.next_runs("30 2 * * mon-fri", 2, datetime.datetime(2026, 10, 24, 12, 0))
    assert runs == [datetime.datetime(2026, 10, 26, 2, 30), datetime.datetime(2026, 10, 27, 2, 30)]


def test_prune_keeps_the_newest(tmp_path):
    job = Job("j", "t", [], "s")
    for i in range(4):
        path = tmp_path / f"cliente_DB_{i}.zip"
        path.write_text("x")
        os.utime(path, (time.time() + i, time.time() + i))
    (tmp_path / "other_DB_0.zip").write_text("x")
    schedules.prune_backups(job, tmp_path, "cliente", 2)
    # one kept, the new one makes two; another link's zip is not touched
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cliente_DB_3.zip", "other_DB_0.zip"]


async def test_script_gets_the_instance(here, tmp_path):
    instance = here.settings.servers["srv"].instances["test"]
    (instance.options.venv_bin / "python").symlink_to(sys.executable)
    folder = tmp_path / "scripts"
    folder.mkdir()
    (folder / "show.py").write_text(
        "import json, os, sys\n"
        "print('ARGS', sys.argv[1:])\n"
        "print('ENV', json.dumps({k: v for k, v in os.environ.items() if k.startswith('OMNIACLI_')}))\n"
        "print('CWD', os.getcwd())\n"
        "sys.exit(3 if '--fail' in sys.argv else 0)\n")
    job = Job("j", "t", [], "s")
    await schedules.run_script(job, instance, folder, schedules.ScriptStep("show.py", "--db 'a b'"))
    text = "\n".join(job.lines)
    assert "ARGS ['--db', 'a b']" in text and f"CWD {instance.repositories[0].path}" in text
    env = json.loads(text.split("ENV ", 1)[1].splitlines()[0])
    assert env["OMNIACLI_SERVER"] == "srv" and env["OMNIACLI_ROLE"] == "test"
    assert env["OMNIACLI_DATABASE"] == "DB_test" and env["OMNIACLI_ODOO_CONFIG"].endswith("odoo.conf")
    assert set(json.loads(env["OMNIACLI_REPOSITORIES"])) == {"odoo", "addons"}
    with pytest.raises(schedules.JobFailed, match="exited with code 3"):
        await schedules.run_script(job, instance, folder, schedules.ScriptStep("show.py", "--fail"))
    with pytest.raises(schedules.JobFailed, match="no script nope.py"):
        await schedules.run_script(job, instance, folder, schedules.ScriptStep("nope.py"))


async def save(client, name, data, original=None):
    return await client.post("/api/schedules/save", headers=as_user("admin", post=True),
                             json={"original": original, "name": name, "schedule": data})


async def test_page_save_run_delete(here, env):
    page = (await here.get("/schedules", headers=as_user("admin"))).text
    assert "No schedules yet." in page and '"srv/test"' in page
    assert (await here.get("/schedules", headers=as_user("dev"))).status_code == 403
    nxt = (await here.get("/api/schedules/next?cron=0+2+*+*+*", headers=as_user("admin"))).json()
    assert len(nxt["next"]) == 5 and nxt["next"][0].endswith("02:00")
    assert (await here.get("/api/schedules/next?cron=bad", headers=as_user("admin"))).status_code == 400

    # the copy: options as the plugin asks them; 4 zips of the link there already
    folder = here.settings.backup_dir / "srv" / "test"
    folder.mkdir(parents=True)
    for i in range(4):
        (folder / f"cliente_old_{i}.zip").write_text("x")
        os.utime(folder / f"cliente_old_{i}.zip", (time.time() - 100 + i,) * 2)
    resp = await save(here, "nightly", a_schedule(
        pull=True, enabled=False,
        copy={"link": "cliente", "server": "srv", "role": "production", "keep_backups": 2,
              "backup_options": {"no_filestore": True}, "restore_options": {"neutralize": True}}))
    assert resp.status_code == 200, resp.text
    saved = yaml.safe_load(here.settings.schedules_file.read_text())["schedules"]["nightly"]
    assert saved["copy"]["backup_options"] == {"no_filestore": True} and saved["enabled"] is False
    assert (await save(here, "nightly", a_schedule(pull=True))).json()["error"].startswith("A schedule named")
    assert "No instance" in (await save(here, "x", a_schedule(pull=True, instance="nope/test"))).json()["error"]

    resp = await here.post("/api/schedules/nightly/run", headers=as_user("admin", post=True))
    job = await wait(here, resp.json()["job"])
    assert job["status"] == "success", job
    assert job["source"] == "schedule nightly (omniacli)"
    done = calls(env)
    assert " --no-filestore DB_production " in done[0] and done[1] == "stop odoo-test"
    assert "--neutralize DB_test" in done[2]
    # the older zips went first: one old and the new one stay
    assert sorted(p.name.startswith("cliente_old_3") or p.name.startswith("cliente_DB_production_")
                  for p in folder.iterdir()) == [True, True]
    # there, the backup was asked on behalf of omniacli
    there = (await here.get("/links/cliente/api/jobs", headers=as_user("admin"))).json()["jobs"]
    assert any(j["title"].startswith("Backup srv/production") and "(omniacli)" in j["source"] for j in there)

    page = (await here.get("/schedules", headers=as_user("admin"))).text
    assert "copy from cliente › srv/production" in page and "manual: admin" in page and ">success<" in page

    # renamed: the history follows
    data = a_schedule(script=None, pull=True)
    assert (await save(here, "renamed", data, original="nightly")).status_code == 200
    assert "nightly" not in here.settings.schedules_file.read_text()
    assert "manual: admin" in (await here.get("/schedules", headers=as_user("admin"))).text

    resp = await here.post("/api/schedules/delete", headers=as_user("admin", post=True), json={"name": "renamed"})
    assert resp.status_code == 200 and "renamed" not in here.settings.schedules_file.read_text()


async def test_a_link_gone_is_skipped(here):
    resp = await save(here, "c", a_schedule(copy={"link": "cliente", "server": "srv"}))
    assert resp.status_code == 200, resp.text
    here.settings.link_targets.clear()
    resp = await here.post("/api/schedules/c/run", headers=as_user("admin", post=True))
    assert resp.status_code == 400 and "no link cliente" in resp.json()["error"]
    page = (await here.get("/schedules", headers=as_user("admin"))).text
    assert ">skipped<" in page


async def test_the_loop_fires_at_matching_minutes(here):
    assert (await save(here, "every", a_schedule(pull=True, cron="* * * * *"))).status_code == 200
    assert (await save(here, "never", a_schedule(pull=True, cron="0 0 31 2 *"))).status_code == 200
    assert (await save(here, "off", a_schedule(pull=True, cron="* * * * *", enabled=False))).status_code == 200
    from omniacli.jobs import JobManager
    scheduler = schedules.Scheduler(here.settings, JobManager(here.settings.jobs_dir), None,
                                    schedules.ScheduleHistory(here.settings.jobs_dir.parent / "h.json"))
    scheduler.load()
    scheduler.start()
    for _ in range(100):
        if scheduler.history.data:
            break
        await __import__("asyncio").sleep(0.02)
    await scheduler.stop()
    assert list(scheduler.history.data) == ["every"] and scheduler.history.data["every"][0]["trigger"] == "cron"
