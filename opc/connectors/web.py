"""网页连接器：URL → 正文 → 入库。"""
from __future__ import annotations

from typing import Any

import httpx

from ..capture import capture
from ..util import html_to_text
from .base import Connector, Status

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


class WebConnector(Connector):
    name = "web"
    kind = "http"

    def fetch(self, url: str, timeout: float = 30.0) -> tuple[str, str]:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA}) as c:
            r = c.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype:
                text = r.text
                title = url.rsplit("/", 1)[-1] or url
            else:
                text = html_to_text(r.text)
                title = ""
                if text.startswith("# "):
                    title = text.splitlines()[0][2:].strip()
                else:
                    title = url
            return title or url, text

    def ingest(self, url: str, publish: bool | None = None) -> dict[str, Any]:
        title, text = self.fetch(url)
        if len(text.strip()) < 80:
            raise ValueError("抓到的正文太短，可能被反爬拦截")
        return capture(
            kind="web", external_id=url, title=title, text=text, uri=url,
            meta={"fetcher": "httpx"}, publish=publish,
        )

    def status(self) -> Status:
        return Status(self.name, self.kind, True, True, "URL 抓取可用", {})

    def capabilities(self) -> list[str]:
        return ["ingest_url"]
