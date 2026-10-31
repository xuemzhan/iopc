"""连接器基类与注册表。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Status:
    name: str
    kind: str
    enabled: bool
    ok: bool
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "ok": self.ok,
            "detail": self.detail,
            "meta": self.meta,
        }


class Connector:
    name = "base"
    kind = "unknown"

    def __init__(self, conf: dict[str, Any]):
        self.conf = conf or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", False))

    def status(self) -> Status:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "enabled": self.enabled,
                "capabilities": self.capabilities()}

    def capabilities(self) -> list[str]:
        return []
