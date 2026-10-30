# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The editable Odoo parameters of a workspace's instance and its debug log:
the template's bounds or the workspace's, workers x memory against the
workspace's memory, the helper's conf, the role restarted."""
import json
from pathlib import Path

import pytest

from omniacli import workspacehelper, workspaceops
from omniacli.config import ConfigError, parse_workspaces

from .test_app import POST, client, wait_job  # noqa: F401 - the client fixture
from .test_workspace_repositories import instance  # noqa: F401 - the instance fixture
from .test_workspaces import basic, post_as, workspaces  # noqa: F401 - the workspaces fixture

REPO_TEMPLATES = Path(__file__).parent.parent / "templates"


def test_parse_the_bounds():
    ws = parse_workspaces({"acme": {"parameter_bounds": {"workers": ["0", "16"]}}})["acme"]
    assert ws.parameter_bounds == {"workers": [0, 16]} and ws.to_dict()["parameter_bounds"] == {"workers": [0, 16]}
    for bounds, message in (({"workers": [5, 1]}, "min <= max"), ({"workers": ["a", 1]}, "two whole numbers"),
                            ({"Bad-Name": [0, 1]}, "parameter name")):
        with pytest.raises(ConfigError, match=message):
            parse_workspaces({"acme": {"parameter_bounds": bounds}})


@pytest.fixture
def made(instance, monkeypatch):
    """acme-main made from odooplm-20 (its instance.json), the helper's calls recorded."""
    monkeypatch.setattr(workspacehelper, "TEMPLATES", REPO_TEMPLATES)
    folder = instance["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "instance.json").write_text(json.dumps({"template": "odooplm-20", "version": "x", "database": "main"}))
    helper = []

    async def call(job, *args):
        if args[0] != "conf":
            helper.append(args)
            return {}
        helper.append(args)
        confs.append(args)
        helper.append(args)
        return {}
    monkeypatch.setattr(workspaceops, "_helper", call)
    confs = []
    return {**instance, "helper": helper, "confs": confs}


async def test_the_parameters_of_a_role(made, client):
    settings, helper, calls, confs = made["settings"], made["helper"], made["calls"], made["confs"]
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert "Odoo parameters of production" in page and 'data-param="limit_time_cpu"' in page
    url = "/api/workspaces/acme/parameters/save"
    body = {"server": "acme-main", "role": "test", "parameters": {"workers": "2", "limit_memory_hard": "3072",
                                                                    "smtp_password": "s3cret"}, "debug": True}
    resp = await client.post(url, headers=post_as("mario"), json=body)
    assert resp.status_code == 200, resp.text
    job = await wait_job(client, resp.json()["job"])
    assert job["status"] == "success", job["lines"]
    action, *args = confs[-1]
    assert (action, args[:4]) == ("conf", ["acme", settings.workspaces["acme"].storage, "main", "test"])
    # acme has a domain: its names published again (the websocket follows workers)
    assert helper[-1][0] == "web"
    spec = json.loads(args[4])
    assert spec["parameters"]["workers"] == "2" and spec["debug"] is True and spec["bounds"] == {}
    assert calls["restart"] == ["acme-main/test"]
    # the template's bounds; the workspace's when the machine's admins set them
    for parameters, message in (({"workers": "9"}, "between 0 and 8"), ({"data_dir": "/"}, "no editable")):
        resp = await client.post(url, headers=post_as("mario"), json={**body, "parameters": parameters})
        assert resp.status_code == 400 and message in resp.json()["error"], parameters
    settings.workspaces["acme"].parameter_bounds = {"workers": [0, 16]}
    resp = await client.post(url, headers=post_as("mario"), json={**body, "parameters": {"workers": "12"}})
    assert resp.status_code == 200 and json.loads(confs[-1][-1])["bounds"] == {"workers": [0, 16]}
    await wait_job(client, resp.json()["job"])
    # workers x limit_memory_hard within the workspace's memory
    settings.workspaces["acme"].limits = {"memory_gb": 4}
    resp = await client.post(url, headers=post_as("mario"), json=body)
    assert resp.status_code == 400 and "more than the workspace's 4 GB" in resp.json()["error"]
    # a password left empty keeps the one saved (the helper's record of the role)
    (made["folder"] / "test").mkdir(parents=True, exist_ok=True)
    (made["folder"] / "test" / "omniacli.json").write_text(json.dumps({"parameters": {"smtp_password": "Pw-kept-9Z"}}))
    resp = await client.post(url, headers=post_as("mario"), json={**body, "parameters": {"smtp_password": ""}})
    assert resp.status_code == 200
    assert json.loads(confs[-1][-1])["parameters"]["smtp_password"] == "Pw-kept-9Z"
    await wait_job(client, resp.json()["job"])
    page = (await client.get("/workspaces/acme", headers=basic("mario"))).text
    assert "Pw-kept-9Z" not in page.split("Odoo parameters of test")[1].split("</details>")[0]
    # its users and other workspaces' cannot
    assert (await client.post(url, headers=post_as("luca"), json=body)).status_code == 403
    assert (await client.post(url, headers=post_as("eva"), json=body)).status_code == 403
    resp = await client.post(url, headers=post_as("mario"), json={**body, "role": "stage"})
    assert resp.status_code == 400
