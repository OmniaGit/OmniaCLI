# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sample the CPU and memory used by every instance's processes.

A systemd instance is measured on its unit's cgroup (cgroup v2): CPU from
cpu.stat, which covers the main process and every worker, memory as the
resident size of the processes in cgroup.procs. memory.current is not used:
it counts the kernel's file cache too (checked 2026-10-20 on postgresql:
3544 MiB against 298 MiB resident). An init.d instance, or a systemd one whose
cgroup cannot be read, is measured on the processes whose command line names
its config file (-c / --config), the one its plugin gives (Odoo: the Odoo config).

The machine as a whole is sampled too: its CPU from /proc/stat (percent of all
cores, iowait counted as idle) and its network traffic from /proc/net/dev,
summed over the interfaces of a real device (/sys/class/net/<name>/device):
the loopback, bridges, veth pairs and other virtual interfaces would count the
same bytes again. Where no interface has a device (a container), every
interface but the loopback is counted.

Each workspace is sampled on its systemd slice (omniacli_<name>.slice, which
holds all its containers): CPU from cpu.stat, memory as the anonymous memory
of memory.stat (memory.current counts the file cache too, as above), and its
traffic on the bridges of its projects' networks (their /24s, which the root
helper reads from their .env): what a bridge sends is what the workspace's
containers receive, what it receives is what they send. Traffic between the
containers of one project stays on the bridge and is not counted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time
from collections import deque
from pathlib import Path

from .config import Instance, Settings
from . import ops

INTERVAL = 5  # seconds between samples
NETWORKS_EVERY = 60  # seconds between two looks at the workspaces' networks
NET_ROOT = Path("/sys/class/net")
HISTORY = 720  # samples kept: one hour
CGROUP_ROOT = Path("/sys/fs/cgroup")
_TICKS = os.sysconf("SC_CLK_TCK")
_PAGE = os.sysconf("SC_PAGE_SIZE")


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _config_args(argv: list[str]) -> list[str]:
    """Every value of -c / --config: python's own -c may come first."""
    values = []
    for i, arg in enumerate(argv):
        if arg in ("-c", "--config") and i + 1 < len(argv):
            values.append(argv[i + 1])
        elif arg.startswith("--config="):
            values.append(arg.split("=", 1)[1])
        elif arg.startswith("-c") and len(arg) > 2:
            values.append(arg[2:])
    return values


def processes_by_config(proc: Path = Path("/proc")) -> dict[str, list[int]]:
    """{real path of a config file: pids started with -c that config}."""
    found: dict[str, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        raw = _read(entry / "cmdline")
        if not raw:
            continue
        for conf in _config_args(raw.rstrip("\0").split("\0")):
            if os.path.isabs(conf):
                found.setdefault(os.path.realpath(conf), []).append(int(entry.name))
    return found


def _proc_usage(pids: list[int], proc: Path = Path("/proc")) -> tuple[dict[int, float], int]:
    """({pid: cpu seconds used}, total resident bytes) of the live pids."""
    cpu, rss = {}, 0
    for pid in pids:
        stat = _read(proc / str(pid) / "stat")
        statm = _read(proc / str(pid) / "statm")
        if not stat or not statm:
            continue
        # the fields after the command name, which may contain spaces
        fields = stat.rsplit(")", 1)[1].split()
        cpu[pid] = (int(fields[11]) + int(fields[12])) / _TICKS  # utime + stime
        rss += int(statm.split()[1]) * _PAGE
    return cpu, rss


def machine_counters(proc: Path = Path("/proc"), net: Path = Path("/sys/class/net")) -> tuple[int, int, int, int]:
    """(busy cpu ticks, total cpu ticks, received bytes, sent bytes) so far."""
    fields = [int(v) for v in (proc / "stat").read_text().split("\n", 1)[0].split()[1:]]
    # user nice system idle iowait irq softirq steal: guest is already in user
    total = sum(fields[:8])
    busy = total - fields[3] - fields[4]
    interfaces = {}
    for line in (proc / "net" / "dev").read_text().splitlines()[2:]:
        name, _, values = line.partition(":")
        values = values.split()
        interfaces[name.strip()] = (int(values[0]), int(values[8]))
    real = [n for n in interfaces if (net / n / "device").exists()]
    counted = real or [n for n in interfaces if n != "lo"]
    rx = sum(interfaces[n][0] for n in counted)
    tx = sum(interfaces[n][1] for n in counted)
    return busy, total, rx, tx


def slice_counters(base: Path) -> tuple[float, int] | None:
    """(CPU seconds used so far, anonymous memory in bytes) of a cgroup."""
    stat, memory = _read(base / "cpu.stat"), _read(base / "memory.stat")
    if stat is None or memory is None:
        return None
    usage = int(stat.split("usage_usec", 1)[1].split()[0]) / 1e6
    anon = next((int(line.split()[1]) for line in memory.splitlines() if line.startswith("anon ")), 0)
    return usage, anon


def bridge_counters(interfaces: list[str], net: Path = NET_ROOT) -> tuple[int, int]:
    """(bytes received, bytes sent) so far, summed over interfaces."""
    rx = tx = 0
    for name in interfaces:
        rx += int(_read(net / name / "statistics" / "rx_bytes") or 0)
        tx += int(_read(net / name / "statistics" / "tx_bytes") or 0)
    return rx, tx


def interfaces_of(addresses: list[dict], networks: dict[str, list[str]]) -> dict[str, list[str]]:
    """{workspace: its bridges}: the interfaces (ip -j addr) whose network is
    one of its projects'."""
    by_net = {}
    for entry in addresses:
        for addr in entry.get("addr_info", []):
            try:
                by_net[str(ipaddress.ip_network(f"{addr['local']}/{addr['prefixlen']}", strict=False))] = entry["ifname"]
            except (KeyError, ValueError):
                continue
    return {ws: [by_net[n] for n in nets if n in by_net] for ws, nets in networks.items()}


class Sampler:
    def __init__(self, settings: Settings, networks=None):
        self.settings = settings
        self.history: dict[str, deque] = {}
        # per instance: how it is measured, and the previous counters
        self.source: dict[str, str] = {}
        self._cgroup: dict[str, tuple[float, str | None]] = {}
        self._prev: dict[str, tuple[float, object]] = {}
        self._task: asyncio.Task | None = None
        self.cores = os.cpu_count() or 1
        self.machine: deque = deque(maxlen=HISTORY)
        self._machine_prev: tuple[float, tuple[int, int, int, int]] | None = None
        # the workspaces: their points, previous counters, bridges; networks is
        # an async callable giving {workspace: [its projects' networks]}
        self.workspaces: dict[str, deque] = {}
        self._ws_prev: dict[str, tuple[float, float | None, int, int]] = {}
        self._networks = networks
        self._bridges: dict[str, list[str]] = {}
        self._bridges_at = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - sampling must never stop
                pass
            await asyncio.sleep(INTERVAL)

    async def _unit_cgroup(self, instance: Instance) -> str | None:
        """The unit's cgroup, asked to systemd at most once a minute."""
        cached = self._cgroup.get(instance.key)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        code, out = await ops.capture(
            ["systemctl", "show", "-p", "ControlGroup", "--value", instance.restart.service],
            timeout=10,
        )
        group = out if code == 0 and out.startswith("/") else None
        self._cgroup[instance.key] = (time.monotonic(), group)
        return group

    def sample_machine(self) -> None:
        now, counters = time.monotonic(), machine_counters()
        previous, self._machine_prev = self._machine_prev, (now, counters)
        if previous is None or now <= previous[0]:
            return
        seconds = now - previous[0]
        busy, total, rx, tx = (c - p for c, p in zip(counters, previous[1]))
        self.machine.append({
            "t": int(time.time()),
            "cpu": round(busy / total * 100, 1) if total > 0 else None,  # percent of all cores
            # bytes per second; a counter that went back (interface reset) is no traffic
            "rx": round(max(rx, 0) / seconds),
            "tx": round(max(tx, 0) / seconds),
        })

    async def _refresh_bridges(self) -> None:
        """Which bridges are each workspace's, looked at once a minute."""
        if self._networks is None or time.monotonic() - self._bridges_at < NETWORKS_EVERY:
            return
        self._bridges_at = time.monotonic()
        try:
            networks = await self._networks()
            code, out = await ops.capture(["ip", "-j", "-4", "addr", "show"], timeout=10)
            self._bridges = interfaces_of(json.loads(out) if code == 0 else [], networks)
        except Exception:  # noqa: BLE001 - no helper, no ip: no traffic shown
            self._bridges = {}

    def sample_workspaces(self) -> None:
        from .workspacehelper import slice_name  # the helper's own name of the slice

        now = time.monotonic()
        names = set(self.settings.workspaces)
        for name in names:
            counters = slice_counters(CGROUP_ROOT / slice_name(name))
            rx, tx = bridge_counters(self._bridges.get(name, []), NET_ROOT)
            previous = self._ws_prev.get(name)
            self._ws_prev[name] = (now, counters[0] if counters else None, rx, tx)
            point = {"t": int(time.time()), "cpu": None, "mem": None, "in": None, "out": None,
                     "bridges": len(self._bridges.get(name, []))}
            if counters:
                point["mem"] = round(counters[1] / 1048576, 1)  # MiB
            if previous is not None and now > previous[0]:
                seconds = now - previous[0]
                if counters and previous[1] is not None:
                    point["cpu"] = round(max(counters[0] - previous[1], 0) / seconds * 100, 1)  # percent of one core
                if self._bridges.get(name):
                    # a bridge sends to the containers what they receive
                    point["in"] = round(max(tx - previous[3], 0) / seconds)
                    point["out"] = round(max(rx - previous[2], 0) / seconds)
            self.workspaces.setdefault(name, deque(maxlen=HISTORY)).append(point)
        for name in list(self.workspaces):
            if name not in names:
                self.workspaces.pop(name, None)
                self._ws_prev.pop(name, None)

    def workspace_series(self, name: str, since: float = 0) -> list[dict]:
        """The points of workspace name after since, the last hour at most."""
        return [p for p in self.workspaces.get(name, ()) if p["t"] > since]

    def workspace_now(self) -> dict[str, dict]:
        """The last point of each workspace."""
        return {name: points[-1] for name, points in self.workspaces.items() if points}

    async def sample(self) -> None:
        try:
            await asyncio.to_thread(self.sample_machine)
        except (OSError, ValueError, IndexError):
            pass  # no /proc: the instances are still sampled
        if self.settings.workspaces:
            await self._refresh_bridges()
            try:
                await asyncio.to_thread(self.sample_workspaces)
            except (OSError, ValueError, IndexError):
                pass
        instances = self.settings.all_instances()
        by_config = None
        now = time.monotonic()
        keys = set()
        for inst in instances:
            keys.add(inst.key)
            point = None
            if inst.restart.type == "systemd":
                group = await self._unit_cgroup(inst)
                base = CGROUP_ROOT / group.lstrip("/") if group else None
                stat = _read(base / "cpu.stat") if base else None
                procs = _read(base / "cgroup.procs") if base else None
                if stat and procs is not None:
                    usage = int(stat.split("usage_usec", 1)[1].split()[0]) / 1e6
                    pids = [int(p) for p in procs.split()]
                    _, rss = await asyncio.to_thread(_proc_usage, pids)
                    point = self._point(inst.key, now, usage, rss, len(pids))
                    self.source[inst.key] = f"systemd unit {inst.restart.service}"
            if point is None:
                if by_config is None:
                    by_config = await asyncio.to_thread(processes_by_config)
                config = inst.plugin.process_config(inst)
                pids = by_config.get(os.path.realpath(config), []) if config is not None else []
                cpu, rss = await asyncio.to_thread(_proc_usage, pids)
                point = self._point(inst.key, now, cpu, rss, len(cpu))
                self.source[inst.key] = (f"processes started with -c {config}" if config is not None
                                         else f"{inst.plugin.label} gives no way to find its processes")
            self.history.setdefault(inst.key, deque(maxlen=HISTORY)).append(point)
        for key in list(self.history):
            if key not in keys:  # the server was removed or set to draft
                self.history.pop(key, None)
                self._prev.pop(key, None)
                self.source.pop(key, None)

    def _point(self, key: str, now: float, cpu, memory: int, procs: int) -> dict:
        """cpu: seconds used so far (cgroup) or {pid: seconds} (processes)."""
        previous = self._prev.get(key)
        self._prev[key] = (now, cpu)
        percent = None
        if previous is not None and now > previous[0]:
            before = previous[1]
            if isinstance(cpu, dict):
                # only the pids alive in both samples: a new worker has no delta
                used = sum(cpu[p] - before[p] for p in cpu if isinstance(before, dict) and p in before)
            else:
                used = cpu - before if not isinstance(before, dict) else None
            if used is not None:
                percent = round(max(used, 0) / (now - previous[0]) * 100, 1)
        return {
            "t": int(time.time()),
            "cpu": percent,  # percent of one core
            "mem": round(memory / 1048576, 1),  # MiB
            "procs": procs,
        }

    def machine_series(self, since: int = 0) -> list[dict]:
        return [p for p in self.machine if p["t"] > since]

    def series(self, key: str, since: int = 0) -> list[dict]:
        return [p for p in self.history.get(key, ()) if p["t"] > since]
