# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The daily history of the disks and the forecast of the day each is full."""
import datetime
import json

from omniacli import diskhistory
from omniacli.diskhistory import DiskHistory, DiskRecorder, forecast

from .test_app import AUTH, client  # noqa: F401

GIB = 1 << 30
DAY0 = datetime.date(2026, 1, 1)


def disk(used, free, mount="/srv", total=None):
    return {"device": "/dev/sdb1", "mount": mount, "type": "ext4", "used": used, "free": free,
            "total": total or used + free}


def points(per_day, days, start_used=100 * GIB, capacity=1000 * GIB):
    return [[(DAY0 + datetime.timedelta(days=i)).isoformat(), start_used + per_day * i,
             capacity - start_used - per_day * i, capacity] for i in range(days)]


def test_the_full_day_of_a_steady_growth():
    # 10 GiB a day from 100 GiB of 1000: 90 days after the first day
    found = forecast(points(10 * GIB, 30))
    assert found["full"] == (DAY0 + datetime.timedelta(days=90)).isoformat()
    assert found["per_day"] == 10 * GIB and found["days"] == 30


def test_only_the_last_90_days_count():
    # a year of nothing, then 90 days of 5 GiB a day
    flat = points(0, 300)
    grow = [[(DAY0 + datetime.timedelta(days=300 + i)).isoformat(), 100 * GIB + 5 * GIB * i,
             900 * GIB - 5 * GIB * i, 1000 * GIB] for i in range(1, 91)]
    found = forecast(flat + grow)
    # the window: the 90 days back from the last (day 300, flat, is not recorded)
    assert found["per_day"] == 5 * GIB and found["days"] == 90


def test_no_date():
    assert forecast([])["reason"] == "no data yet"
    assert forecast(points(GIB, 6))["reason"] == "a forecast needs 7 days of data"
    shrinking = forecast(points(-GIB, 20))
    assert shrinking["full"] is None and shrinking["reason"] == "the used space is not growing"
    assert forecast(points(0, 20))["reason"] == "the used space is not growing"
    assert forecast(points(1, 20))["reason"] == "not within 100 years at this pace"


def test_record_once_a_day_and_keep_three_years(tmp_path):
    path = tmp_path / "disk_history.json"
    history = DiskHistory(path)
    assert not history.recorded(DAY0)
    history.record([disk(10 * GIB, 90 * GIB), disk(1 * GIB, 9 * GIB, "/")], DAY0)
    assert history.recorded(DAY0)
    # the same day again replaces its point
    history.record([disk(11 * GIB, 89 * GIB)], DAY0)
    again = DiskHistory(path)
    assert again.disks["/srv"]["points"] == [[DAY0.isoformat(), 11 * GIB, 89 * GIB, 100 * GIB]]
    assert [s["mount"] for s in again.series()] == ["/", "/srv"]
    # three years later the first day is gone, and with it a disk no longer seen
    later = DAY0 + datetime.timedelta(days=diskhistory.KEEP_DAYS + 1)
    again.record([disk(20 * GIB, 80 * GIB)], later)
    assert [p[0] for p in again.disks["/srv"]["points"]] == [later.isoformat()]
    assert "/" not in again.disks
    assert json.loads(path.read_text())["disks"].keys() == {"/srv"}


def test_a_broken_file_starts_a_new_history(tmp_path):
    path = tmp_path / "disk_history.json"
    path.write_text("not json")
    assert DiskHistory(path).disks == {}


async def test_the_recorder_records_today_once(env, tmp_path, monkeypatch):
    found = [disk(10 * GIB, 90 * GIB)]

    async def fake_disks(settings):
        return found
    monkeypatch.setattr(diskhistory._disks, "disks", fake_disks)
    recorder = DiskRecorder(env["settings"], DiskHistory(tmp_path / "h.json"))
    assert await recorder.check(DAY0) is True
    assert await recorder.check(DAY0) is False
    assert await recorder.check(DAY0 + datetime.timedelta(days=1)) is True
    assert len(recorder.history.disks["/srv"]["points"]) == 2


async def test_api_and_page(client, env, monkeypatch):
    history = client.app.state.disk_history
    # a mount this machine has not: the recorder adds today's real disks
    mount = "/omniacli-test-disk"
    for p in points(10 * GIB, 30):
        history.record([disk(p[1], p[2], mount)], datetime.date.fromisoformat(p[0]))
    data = (await client.get("/api/disks/history", headers={"Authorization": AUTH})).json()
    entry = next(d for d in data["disks"] if d["mount"] == mount)
    assert len(entry["points"]) == 30 and entry["forecast"]["full"] == "2026-04-01"
    # the disks panel gets each disk's forecast (None before its first day)
    live = (await client.get("/api/disks", headers={"Authorization": AUTH})).json()["disks"]
    assert live and all("forecast" in d for d in live)
    history.record([disk(1, 2, live[0]["mount"])], datetime.date(2020, 1, 1))
    live = (await client.get("/api/disks", headers={"Authorization": AUTH})).json()["disks"]
    assert live[0]["forecast"]["reason"]
    page = await client.get("/disks", headers={"Authorization": AUTH})
    assert page.status_code == 200 and "/api/disks/history" in page.text


def test_the_workspaces_disks_are_recorded_apart(tmp_path):
    path = tmp_path / "disk_history.json"
    history = DiskHistory(path)
    history.record([disk(10 * GIB, 90 * GIB)], DAY0)
    assert not history.workspaces_recorded(DAY0)
    # 1 GiB a day in a disk of 50 GiB from 10: full 40 days after the first
    for i in range(10):
        history.record_workspaces([{"name": "acme", "used": 10 * GIB + i * GIB, "capacity": 50 * GIB}],
                                  DAY0 + datetime.timedelta(days=i))
    again = DiskHistory(path)
    assert again.workspaces_recorded(DAY0 + datetime.timedelta(days=9))
    assert again.workspace_forecast("acme")["full"] == (DAY0 + datetime.timedelta(days=40)).isoformat()
    assert again.workspace_forecast("none") is None
    assert [d["name"] for d in again.workspace_series()] == ["acme"] and again.workspace_series({"beta"}) == []
    # the machine's disks are as they were: the publisher sends them only
    assert [s["mount"] for s in again.series()] == ["/srv"]
    # used over its limit (forced): no free space, never a negative one
    again.record_workspaces([{"name": "acme", "used": 60 * GIB, "capacity": 50 * GIB}], DAY0 + datetime.timedelta(days=10))
    assert again.workspaces["acme"]["points"][-1][2] == 0


async def test_the_recorder_records_the_workspaces(env, tmp_path, monkeypatch):
    from omniacli.config import Workspace

    async def fake_disks(settings):
        return [disk(10 * GIB, 90 * GIB)]
    monkeypatch.setattr(diskhistory._disks, "disks", fake_disks)
    env["settings"].workspaces = {"acme": Workspace("acme")}
    calls = []

    async def workspaces():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("sudo: a password is required")
        return [{"name": "acme", "used": GIB, "capacity": 10 * GIB}]
    recorder = DiskRecorder(env["settings"], DiskHistory(tmp_path / "h.json"), workspaces=workspaces)
    # no helper: the machine's disks are recorded all the same, the workspaces tried again
    assert await recorder.check(DAY0) is True
    assert recorder.history.recorded(DAY0) and not recorder.history.workspaces_recorded(DAY0)
    assert await recorder.check(DAY0) is True
    assert recorder.history.workspaces_recorded(DAY0)
    assert await recorder.check(DAY0) is False and len(calls) == 2


async def test_every_disk_page_uses_the_widget(client, env):
    # one widget for every disk shown: no page draws a disk of its own
    for path in ("/", "/disks"):
        page = (await client.get(path, headers={"Authorization": AUTH})).text
        assert "const OmniaDisk" in page and "OmniaDisk.render" in page, path
    data = (await client.get("/api/disks/history", headers={"Authorization": AUTH})).json()
    assert "workspaces" in data
