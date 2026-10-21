# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sample the CPU and memory used by every instance's Odoo processes.

A systemd instance is measured on its unit's cgroup (cgroup v2): CPU from
cpu.stat, which covers the main process and every worker, memory as the
resident size of the processes in cgroup.procs. memory.current is not used:
it counts the kernel's file cache too (checked 2026-10-20 on postgresql:
3544 MiB against 298 MiB resident). An init.d instance, or a systemd one whose
cgroup cannot be read, is measured on the processes whose command line names
its Odoo config file (-c / --config).

The machine as a whole is sampled too: its CPU from /proc/stat (percent of all
cores, iowait counted as idle) and its network traffic from /proc/net/dev,
summed over the interfaces of a real device (/sys/class/net/<name>/device):
the loopback, bridges, veth pairs and other virtual interfaces would count the
same bytes again. Where no interface has a device (a container), every
interface but the loopback is counted.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from pathlib import Path

from .config import Instance, Settings
from . import ops

INTERVAL = 5  # seconds between samples
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
    """{real path of an Odoo config: pids started with -c that config}."""
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


class Sampler:
    def __init__(self, settings: Settings):
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

    async def sample(self) -> None:
        try:
            await asyncio.to_thread(self.sample_machine)
        except (OSError, ValueError, IndexError):
            pass  # no /proc: the instances are still sampled
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
                pids = by_config.get(os.path.realpath(inst.odoo_config), [])
                cpu, rss = await asyncio.to_thread(_proc_usage, pids)
                point = self._point(inst.key, now, cpu, rss, len(cpu))
                self.source[inst.key] = f"processes started with -c {inst.odoo_config}"
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
