# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The load of each workspace on the Workspaces page: CPU and memory of its
systemd slice, traffic of its projects' bridges, its disk."""
import json

from omniacli import metrics
from omniacli.config import Settings, Workspace

from .test_app import AUTH, client  # noqa: F401 - the client fixture
from .test_workspaces import basic, workspaces  # noqa: F401 - the workspaces fixture


def test_slice_counters_and_bridges(tmp_path):
    base = tmp_path / "omniacli_acme.slice"
    base.mkdir()
    (base / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 2000000\n")
    (base / "memory.stat").write_text("anon 104857600\nfile 999999999\nanon_thp 0\n")
    assert metrics.slice_counters(base) == (2.5, 104857600)
    assert metrics.slice_counters(tmp_path / "none") is None
    net = tmp_path / "net"
    for name, rx, tx in (("br-a", 100, 200), ("br-b", 1, 2)):
        (net / name / "statistics").mkdir(parents=True)
        (net / name / "statistics" / "rx_bytes").write_text(f"{rx}\n")
        (net / name / "statistics" / "tx_bytes").write_text(f"{tx}\n")
    assert metrics.bridge_counters(["br-a", "br-b"], net) == (101, 202)
    addresses = [{"ifname": "eth0", "addr_info": [{"local": "192.168.1.5", "prefixlen": 24}]},
                 {"ifname": "br-a", "addr_info": [{"local": "10.213.0.1", "prefixlen": 24}]},
                 {"ifname": "br-b", "addr_info": [{"local": "10.213.1.1", "prefixlen": 24}]}]
    assert metrics.interfaces_of(addresses, {"acme": ["10.213.0.0/24", "10.213.9.0/24"], "beta": ["10.213.1.0/24"]}) \
        == {"acme": ["br-a"], "beta": ["br-b"]}


def test_a_workspace_is_sampled(tmp_path, monkeypatch):
    settings = Settings(users={}, backup_dir=tmp_path, jobs_dir=tmp_path, servers_file=tmp_path / "s.yaml", servers={},
                        workspaces={"acme": Workspace("acme"), "beta": Workspace("beta")})
    cg, net = tmp_path / "cg", tmp_path / "net"
    (cg / "omniacli_acme.slice").mkdir(parents=True)
    (net / "br-a" / "statistics").mkdir(parents=True)
    monkeypatch.setattr(metrics, "CGROUP_ROOT", cg)
    monkeypatch.setattr(metrics, "NET_ROOT", net)
    clock = iter([100.0, 110.0])
    monkeypatch.setattr(metrics.time, "monotonic", lambda: next(clock))

    def counters(cpu_usec, anon, rx, tx):
        (cg / "omniacli_acme.slice" / "cpu.stat").write_text(f"usage_usec {cpu_usec}\n")
        (cg / "omniacli_acme.slice" / "memory.stat").write_text(f"anon {anon}\n")
        (net / "br-a" / "statistics" / "rx_bytes").write_text(f"{rx}\n")
        (net / "br-a" / "statistics" / "tx_bytes").write_text(f"{tx}\n")
    sampler = metrics.Sampler(settings)
    sampler._bridges = {"acme": ["br-a"]}
    counters(1_000_000, 1048576, 1000, 5000)
    sampler.sample_workspaces()
    counters(6_000_000, 2097152, 3000, 15000)
    sampler.sample_workspaces()
    now = sampler.workspace_now()
    # 5 s of CPU in 10 s: half a core; the bridge sent 10000 B (in), received 2000 B (out)
    assert now["acme"]["cpu"] == 50.0 and now["acme"]["mem"] == 2.0
    assert now["acme"]["in"] == 1000 and now["acme"]["out"] == 200 and now["acme"]["bridges"] == 1
    # no slice, no bridges yet: nothing measured, nothing made up
    assert now["beta"]["cpu"] is None and now["beta"]["mem"] is None and now["beta"]["in"] is None
    # a workspace deleted leaves the samples
    settings.workspaces.pop("beta")
    monkeypatch.setattr(metrics.time, "monotonic", lambda: 120.0)
    sampler.sample_workspaces()
    assert set(sampler.workspace_now()) == {"acme"}


async def test_the_list_and_its_load(workspaces, client, env):  # noqa: F811
    settings = env["settings"]
    page = (await client.get("/workspaces", headers={"Authorization": AUTH})).text
    assert 'class="ws-row" data-ws="acme"' in page and 'class="ws-row" data-ws="beta"' in page
    assert "New workspace" in page and 'id="edit-view" hidden' in page
    resp = await client.get("/api/workspaces/load", headers={"Authorization": AUTH})
    assert resp.status_code == 200 and set(resp.json()) == {"cores", "interval", "machine", "workspaces"}
    # a user of workspaces: refused, as the page
    assert (await client.get("/api/workspaces/load", headers=basic("mario"))).status_code == 403
    assert settings.workspaces["acme"].name == "acme"


def test_space_tells_the_networks_of_a_workspace(tmp_path, monkeypatch):
    from omniacli import workspacehelper

    storage = tmp_path / "storage"
    (storage / ".disks").mkdir(parents=True)
    (storage / ".disks" / "acme.img").write_bytes(b"x")
    for role, subnet in (("production", "10.213.0.0/24"), ("test", "10.213.1.0/24")):
        (storage / "acme" / "main" / role).mkdir(parents=True)
        (storage / "acme" / "main" / role / ".env").write_text(f"PROJECT=acme-main-{role}\nSUBNET={subnet}\n")
    monkeypatch.setattr(workspacehelper, "storage_roots", lambda config=None: (str(storage),))
    found = workspacehelper.space()["roots"][0]
    assert found["networks"] == {"acme": ["10.213.0.0/24", "10.213.1.0/24"]}
    assert json.dumps(found)
