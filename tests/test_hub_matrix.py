"""WP-08：Hub 访问控制矩阵 —— 所有 /api 路由的令牌 / Origin 行为一致。"""
from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

import opc.config as config_mod
from opc.config import Config
from opc.hub.app import PUBLIC_PATHS, app, token_guard


def _next_ok(_request):
    return JSONResponse({"ok": True})


async def _next(request):  # noqa: ANN001
    return JSONResponse({"ok": True})


def _api_routes() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if not path.startswith("/api"):
            continue
        import re

        concrete = re.sub(r"\{[^}]+\}", "1", path)
        for m in sorted(methods - {"HEAD", "OPTIONS"}):
            out.append((m, concrete))
    return out


def _req(method: str, path: str, *, host: str = "127.0.0.1:5370",
         client: str = "127.0.0.1", token: str | None = None,
         origin: str | None = None, sfs: str | None = None) -> Request:
    headers = [(b"host", host.encode())]
    if token:
        headers.append((b"x-opc-token", token.encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    if sfs:
        headers.append((b"sec-fetch-site", sfs.encode()))
    return Request({
        "type": "http", "method": method, "scheme": "http", "path": path,
        "query_string": b"", "headers": headers,
        "client": (client, 12345), "server": ("127.0.0.1", 5370),
    })


def _run(req: Request) -> int:
    return asyncio.run(token_guard(req, _next)).status_code


def test_all_api_routes_require_token_from_lan(monkeypatch):
    import os

    monkeypatch.setenv("OPC_TOKEN", "s3cret")
    monkeypatch.setattr(config_mod, "_cfg", Config({"security": {"token": "s3cret"}}))
    routes = _api_routes()
    assert len(routes) >= 70, f"应为全部 API 路由，实际 {len(routes)}"
    bad = [(m, p) for m, p in routes
           if _run(_req(m, p, host="127.0.0.1:5370", client="192.168.1.9")) != 403]
    assert not bad, f"无令牌的 LAN 请求未被拒绝：{bad[:5]}"


def test_all_api_routes_allow_loopback(monkeypatch):
    monkeypatch.delenv("OPC_TOKEN", raising=False)
    monkeypatch.setattr(config_mod, "_cfg", Config({"security": {"token": ""}}))
    bad = [(m, p) for m, p in _api_routes()
           if _run(_req(m, p, host="127.0.0.1:5370", client="127.0.0.1")) != 200]
    assert not bad, f"本机请求被误拒：{bad[:5]}"


def test_all_api_routes_allow_valid_token(monkeypatch):
    monkeypatch.setenv("OPC_TOKEN", "s3cret")
    monkeypatch.setattr(config_mod, "_cfg", Config({"security": {"token": "s3cret"}}))
    bad = [(m, p) for m, p in _api_routes()
           if _run(_req(m, p, host="192.168.1.5:5370", client="192.168.1.9",
                        token="s3cret")) != 200]
    assert not bad, f"带正确令牌仍被拒：{bad[:5]}"


def test_origin_and_sec_fetch_are_enforced(monkeypatch):
    monkeypatch.setenv("OPC_TOKEN", "s3cret")
    monkeypatch.setattr(config_mod, "_cfg", Config({"security": {"token": "s3cret"}}))
    path = "/api/capture"
    assert _run(_req("POST", path, host="192.168.1.5:5370", client="192.168.1.9",
                     token="s3cret", origin="http://evil.example")) == 403
    assert _run(_req("POST", path, host="192.168.1.5:5370", client="192.168.1.9",
                     token="s3cret", sfs="cross-site")) == 403
    assert _run(_req("POST", path, host="192.168.1.5:5370", client="192.168.1.9",
                     token="s3cret", origin="http://192.168.1.5:5370")) == 200


def test_public_static_and_shell_open_but_api_needs_token(monkeypatch):
    """LAN 场景：壳与静态资源免令牌（页面要能打开并提示输令牌），API 仍需令牌。"""
    monkeypatch.setenv("OPC_TOKEN", "s3cret")
    monkeypatch.setattr(config_mod, "_cfg", Config({"security": {"token": "s3cret"}}))
    assert "/sw.js" in PUBLIC_PATHS
    assert _run(_req("GET", "/sw.js", host="192.168.1.5:5370",
                     client="192.168.1.9")) == 200
    assert _run(_req("GET", "/", host="192.168.1.5:5370",
                     client="192.168.1.9")) == 200
    assert _run(_req("GET", "/api/health", host="192.168.1.5:5370",
                     client="192.168.1.9")) == 403
    assert _run(_req("GET", "/api/health", host="192.168.1.5:5370",
                     client="192.168.1.9", token="s3cret")) == 200
