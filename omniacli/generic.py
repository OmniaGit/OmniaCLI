# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generic type: git repositories and a systemd or init.d service,
nothing else. It is what OmniaCLI offers without any plugin installed.

Like the Odoo plugin, it imports from OmniaCLI only omniacli.plugin_api.
"""

from __future__ import annotations

from .plugin_api import PLUGIN_API, Check, Instance, Job, ServicePlugin


class GenericPlugin(ServicePlugin):
    name = "generic"
    label = "Generic service"
    api_version = PLUGIN_API

    def parse_options(self, data: dict, where: str, restart_type: str) -> dict:
        return {}

    def options_to_dict(self, options: dict) -> dict:
        return {}

    async def checks(self, instance: Instance) -> list[Check]:
        return []

    async def after_pull(self, job: Job, instance: Instance) -> None:
        return None
