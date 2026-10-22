# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An administrator stops a job that hangs or loops."""
import asyncio
import os

from omniacli import ops
from omniacli.jobs import STOPPED, SUCCESS, JobManager

from .test_app import AUTH, HOST, POST, basic, client, wait_job  # noqa: F401


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is dead: only its parent has not reaped it yet
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


async def until(condition, seconds=10):
    for _ in range(int(seconds * 20)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_stop_a_looping_job_and_its_children(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "KILL_WAIT", 1)
    jobs = JobManager(tmp_path / "jobs")
    pids = tmp_path / "pids"
    # a loop that ignores SIGTERM, with a child that ignores it too
    script = f'trap "" TERM; sleep 1000 & echo $$ $! > {pids}; while :; do sleep 1; done'
    looping = jobs.submit("loop", ["s/test"], "test", lambda job: ops.run(job, ["sh", "-c", script]))
    after = jobs.submit("after", ["s/test"], "test", lambda job: ops.run(job, ["true"]))
    await until(lambda: pids.exists() and pids.read_text().strip())
    shell, child = map(int, pids.read_text().split())
    assert jobs.is_busy("s/test") and after.status == "queued"

    assert jobs.stop(looping.id, "admin")
    await until(lambda: looping.finished_flag)
    assert looping.status == STOPPED and looping.error == "stopped by admin"
    assert any("stop asked by admin" in line for line in looping.lines)
    assert any("STOPPED by admin" in line for line in looping.lines)
    # the whole group, though it ignores SIGTERM: SIGKILL after KILL_WAIT
    await until(lambda: not alive(shell) and not alive(child))
    # the instance is free again: the next job runs
    await until(lambda: after.finished_flag)
    assert after.status == SUCCESS
    assert not jobs.stop(looping.id, "admin")  # finished: nothing to stop


async def test_stop_a_queued_job(tmp_path):
    jobs = JobManager(tmp_path / "jobs")
    first = jobs.submit("first", ["s/test"], "test", lambda job: ops.run(job, ["sleep", "30"]))
    queued = jobs.submit("queued", ["s/test", "s/production"], "test", lambda job: ops.run(job, ["true"]))
    await until(lambda: first.status == "running")
    assert jobs.stop(queued.id, "admin")
    await until(lambda: queued.finished_flag)
    assert queued.status == STOPPED and queued.started is None
    assert first.status == "running" and not jobs.is_busy("s/production")
    assert jobs.stop(first.id, "admin")
    await until(lambda: first.finished_flag)
    assert not jobs.is_busy("s/test")


async def test_only_an_admin_stops_a_job(client, env):
    settings = client.app.state.settings
    jobs = client.app.state.jobs
    inst = settings.instance("srv", "test")
    job = jobs.submit("Hangs", [inst.key], "test", lambda job: ops.run(job, ["sleep", "30"]))
    await until(lambda: job.status == "running")

    page = (await client.get(f"/jobs/{job.id}", headers={"Authorization": AUTH})).text
    assert f'action="/jobs/{job.id}/stop"' in page
    dev = {"Authorization": basic("dev"), "Origin": f"http://{HOST}"}
    assert "/stop" not in (await client.get(f"/jobs/{job.id}", headers=dev)).text
    assert (await client.post(f"/jobs/{job.id}/stop", headers=dev)).status_code == 403
    assert (await client.post("/jobs/nope/stop", headers=POST)).status_code == 404

    resp = await client.post(f"/jobs/{job.id}/stop", headers=POST)
    assert resp.status_code == 303 and resp.headers["location"] == f"/jobs/{job.id}"
    data = await wait_job(client, job.id)
    assert data["status"] == "stopped" and data["error"] == "stopped by admin"
    assert (await client.post(f"/jobs/{job.id}/stop", headers=POST)).status_code == 409
    assert f'action="/jobs/{job.id}/stop"' not in (await client.get(f"/jobs/{job.id}", headers={"Authorization": AUTH})).text
