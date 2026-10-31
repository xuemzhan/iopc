"""WP-06：能力注册表 —— 四入口清单同源与漂移门禁。"""
from __future__ import annotations

import opc.capabilities as caps


def test_registry_matches_all_four_surfaces():
    assert caps.validate() == [], caps.validate()


def test_registry_covers_every_mcp_tool_and_cli_command():
    from opc.cli import app as cli_app
    from opc.mcp_server import TOOLS

    assert caps.mcp_names() == {t["name"] for t in TOOLS}
    cli_commands = {c.name or c.callback.__name__ for c in cli_app.registered_commands}
    cli_commands |= {g.name for g in cli_app.registered_groups}
    assert caps.cli_names() == cli_commands


def test_registry_summary_shape():
    s = caps.summary()
    assert s["total"] == len(caps.CAPABILITIES) >= 30
    assert s["by_kind"].get("write", 0) and s["by_kind"].get("read", 0)
    ids = {c["id"] for c in s["capabilities"]}
    assert {"capture", "ask", "ontology_merge", "caps"} <= ids
    # REST 字段是结构化对象，便于 UI 直接渲染
    for c in s["capabilities"]:
        if c["rest"]:
            assert set(c["rest"]) == {"method", "path"}


def test_capabilities_rest_endpoint(env, monkeypatch):
    from fastapi.testclient import TestClient

    from opc.hub import app as hub

    _, database, _ = env
    monkeypatch.setattr(hub, "db", lambda: database)
    client = TestClient(hub.app)
    r = client.get("/api/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 30
    assert any(c["id"] == "caps" for c in body["capabilities"])


def test_capabilities_mcp_tool(env, monkeypatch):
    import opc.mcp_server as mcp

    _, database, _ = env
    monkeypatch.setattr(mcp, "db", lambda: database)
    res = mcp._call("opc_capabilities", {})
    assert res["total"] == len(caps.CAPABILITIES)
