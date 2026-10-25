# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
import base64
import logging
import re

from omniacli import selflog

from .conftest import PASSWORD
from .test_app import AUTH, client  # noqa: F401 - the fixture

# what the log viewer reads as a record with its level (_logview.html)
RECORD = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \d+ (\w+) ")


def test_attach_detach(tmp_path, monkeypatch):
    # the level uvicorn gives its access logger when it starts
    monkeypatch.setattr(logging.getLogger("uvicorn.access"), "level", logging.INFO)
    before = {name: list(logging.getLogger(name).handlers) for name in selflog.LOGGERS}
    added = selflog.attach(tmp_path / "logs" / "omniacli.log")
    try:
        logging.getLogger("omniacli.something").info("an info")
        logging.getLogger("uvicorn.access").info("GET / 200")
        logging.getLogger("httpx").warning("a warning")
    finally:
        selflog.detach(added)
    lines = (tmp_path / "logs" / "omniacli.log").read_text().splitlines()
    assert [RECORD.match(line).group(1) for line in lines] == ["INFO", "INFO", "WARNING"]
    assert lines[0].endswith("omniacli.something: an info")
    assert {name: list(logging.getLogger(name).handlers) for name in selflog.LOGGERS} == before


async def test_own_log_page(client, env):  # noqa: F811
    logging.getLogger("omniacli.test").error("something broke")
    resp = await client.get("/api/omniacli/log", headers={"Authorization": AUTH})
    data = resp.json()
    assert resp.status_code == 200, data
    assert any(line.endswith("ERROR omniacli.test: something broke") for line in data["lines"])
    again = await client.get(f"/api/omniacli/log?pos={data['pos']}&inode={data['inode']}",
                             headers={"Authorization": AUTH})
    assert again.json()["lines"] == []
    page = await client.get("/omniacli/log", headers={"Authorization": AUTH})
    assert page.status_code == 200 and str(env["settings"].logs_dir / "omniacli.log") in page.text
    assert 'href="/omniacli/log"' in page.text
    developer = {"Authorization": "Basic " + base64.b64encode(f"dev:{PASSWORD}".encode()).decode()}
    assert (await client.get("/api/omniacli/log", headers=developer)).status_code == 403
    assert (await client.get("/omniacli/log", headers=developer)).status_code == 403
    assert 'href="/omniacli/log"' not in (await client.get("/", headers=developer)).text
