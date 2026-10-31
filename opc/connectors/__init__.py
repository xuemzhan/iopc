"""连接器注册表。新增一个工具 = 新增一个类 + 在 REGISTRY 里登记。"""
from __future__ import annotations

from ..config import cfg
from .base import Connector, Status
from .files import FilesConnector
from .harvest import OpenCodeConnector, PiConnector, WorkBuddyConnector
from .imeeting import IMeetingConnector
from .mindmap_conn import MindmapConnector
from .obsidian import ObsidianConnector
from .web import WebConnector

REGISTRY: dict[str, type[Connector]] = {
    "imeeting": IMeetingConnector,
    "obsidian": ObsidianConnector,
    "opencode": OpenCodeConnector,
    "pi_desktop": PiConnector,
    "workbuddy": WorkBuddyConnector,
    "mindmap": MindmapConnector,
    "files": FilesConnector,
    "web": WebConnector,
}


def connectors() -> dict[str, Connector]:
    c = cfg()
    out: dict[str, Connector] = {}
    for name, cls in REGISTRY.items():
        conf = c.tool(name)
        if name == "web":
            conf = {"enabled": True, **conf}
        out[name] = cls(conf)
    return out


def get(name: str) -> Connector:
    try:
        return connectors()[name]
    except KeyError as exc:
        raise KeyError(f"未知连接器：{name}；可用：{', '.join(REGISTRY)}") from exc


def status_all() -> list[dict]:
    result: list[dict] = []
    for name, conn in connectors().items():
        try:
            st: Status = conn.status()
        except Exception as exc:  # noqa: BLE001
            st = Status(name, conn.kind, conn.enabled, False, f"状态检查失败：{exc}")
        d = st.as_dict()
        d["capabilities"] = conn.capabilities()
        result.append(d)
    return result
