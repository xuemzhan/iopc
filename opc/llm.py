"""LLM 客户端：OpenAI 兼容协议 + 按任务路由 + 预算护栏。

- `for_task("extract")` 按 `[llm.routes]` 选模型；provider/model 缺失时回退到旧 `[llm]` 扁平配置
- 密钥惰性读取：某个 provider 没配 key，只有真正用到它时才报错，进程照常启动
- 每次调用写 `llm_usage` 表（task/provider/model/tokens），预算按当日累计判断
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

from .config import cfg

# 批量类任务：超预算时可以拒绝；人工单条（capture/ask）永远放行
BATCH_TASKS = {"extract", "digest", "verdict", "enrich", "embed", "brief"}

_warned: set[str] = set()


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(
        self,
        *,
        provider: str = "deepseek",
        model: str = "deepseek-chat",
        base_url: str = "",
        api_key_env: str = "DEEPSEEK_API_KEY",
        json_mode: bool = False,
        task: str = "default",
        dim: int | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
    ):
        c = cfg()
        self.provider = provider
        self.model = model
        self.base_url = (base_url or c.get("llm.base_url", "")).rstrip("/")
        self.api_key_env = api_key_env
        self.supports_json = json_mode
        self.task = task
        self.dim = dim
        self.timeout = timeout or float(c.get("llm.timeout_s", 240))
        self.temperature = float(
            temperature if temperature is not None else c.get("llm.temperature", 0.2)
        )
        self.last_usage: dict[str, int] = {}
        self.last_finish_reason: str = ""
        self.sensitive = False
        self.source_id: int | None = None
        self._api_key: str | None = None

    # ---------- 配置 ----------
    def api_key(self) -> str:
        if self._api_key is None:
            key = os.environ.get(self.api_key_env, "")
            if not key:
                raise LLMError(f"缺少密钥：环境变量 {self.api_key_env}（写入仓库根目录的 .env）")
            self._api_key = key
        return self._api_key

    @classmethod
    def for_task(cls, task: str = "default") -> "LLM":
        c = cfg()
        routes = c.section("llm.routes") or {}
        alias = str(routes.get(task) or routes.get("default") or "")
        models = c.section("llm.models") or {}
        spec: dict[str, Any] = models.get(alias, {}) if alias else {}

        provider = str(spec.get("provider") or c.get("llm.provider", "deepseek"))
        prov = c.section(f"llm.providers.{provider}") or {}
        base_url = str(prov.get("base_url") or c.get("llm.base_url", ""))
        key_env = str(prov.get("api_key_env") or c.get("llm.api_key_env", "DEEPSEEK_API_KEY"))
        model = str(spec.get("model") or c.get("llm.model", "deepseek-chat"))
        json_mode = bool(prov.get("json_mode", False))

        key = (provider, model, task)
        inst = _cache.get(key)
        if inst is None:
            inst = cls(provider=provider, model=model, base_url=base_url,
                       api_key_env=key_env, json_mode=json_mode, task=task,
                       dim=spec.get("dim"))
            _cache[key] = inst
        return inst

    # ---------- 预算 ----------
    def _budget_guard(self) -> None:
        if self.task not in BATCH_TASKS:
            return
        try:
            from .usage import today_tokens

            used = today_tokens()
        except Exception:
            return
        max_tokens = int(cfg().get("llm.budget.max_tokens_per_day", 0) or 0)
        if not max_tokens:
            return
        if used >= max_tokens and os.environ.get("OPC_BUDGET_FORCE") != "1":
            raise LLMError(
                f"今日预算已用尽（{used}/{max_tokens} tokens），批量任务暂停；"
                f"如需强制，设置 OPC_BUDGET_FORCE=1"
            )
        ratio = used / max_tokens
        warn_at = float(cfg().get("llm.budget.warn_at_ratio", 0.8) or 0.8)
        if ratio >= warn_at and "budget" not in _warned:
            _warned.add("budget")
            print(f"[opc] 预算提醒：今日已用 {used}/{max_tokens} tokens（{ratio:.0%}）",
                  file=sys.stderr)

    # ---------- 调用 ----------
    def _record(self) -> None:
        usage = self.last_usage or {}
        try:
            from .usage import record_llm

            record_llm(
                task=self.task, provider=self.provider, model=self.model,
                prompt=int(usage.get("prompt_tokens") or 0),
                completion=int(usage.get("completion_tokens") or 0),
                total=int(usage.get("total_tokens") or 0),
                sensitive=self.sensitive, source_id=self.source_id,
            )
        except Exception:
            pass

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool | None = None,
        temperature: float | None = None,
        max_tokens: int = 4096,
        retries: int = 3,
    ) -> str:
        self._budget_guard()
        use_json = bool(json_mode)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if use_json:
            payload["response_format"] = {"type": "json_object"}

        last_err = ""
        for attempt in range(retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    r = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key()}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                    time.sleep(2 + attempt * 4)
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:500]}")
                data = r.json()
                self.last_usage = data.get("usage", {}) or {}
                choice = data["choices"][0]
                self.last_finish_reason = str(choice.get("finish_reason") or "")
                self._record()
                return choice["message"]["content"] or ""
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                time.sleep(2 + attempt * 3)
        raise LLMError(f"LLM 调用失败（重试 {retries} 次）：{last_err}")

    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float | None = None,
    ):
        """逐 token 产出增量文本（SSE）。"""
        self._budget_guard()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as r:
                if r.status_code >= 400:
                    raise LLMError(f"LLM HTTP {r.status_code}: {r.read()[:400]!r}")
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0].get("delta", {}).get("content")
                    except Exception:
                        continue
                    if delta:
                        yield delta

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """JSON 输出；截断或解析失败时降一次温重试（只多花一次调用）。"""
        attempt_msgs = list(messages)
        last_err: Exception | None = None
        for attempt in range(2):
            text = self.chat(
                attempt_msgs,
                json_mode=True,
                max_tokens=max_tokens,
                temperature=0.0 if attempt else temperature,
            )
            if self.last_finish_reason == "length" and attempt == 0:
                attempt_msgs = list(messages) + [
                    {"role": "user", "content": "上次输出被截断了。只输出合法 JSON，不要任何解释。"}
                ]
                continue
            try:
                return parse_json_loose(text)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                attempt_msgs = list(messages) + [
                    {"role": "user", "content": "只输出合法 JSON，不要 markdown 围栏或解释。"}
                ]
        raise LLMError(f"LLM 未返回可解析的 JSON：{last_err}")


def parse_json_loose(text: str) -> dict[str, Any]:
    """容忍 ```json 围栏、前后废话、尾逗号。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"LLM 未返回 JSON：{text[:300]}")
    s = s[start : end + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        import re

        s2 = re.sub(r",\s*([}\]])", r"\1", s)
        return json.loads(s2)


_cache: dict[tuple[str, str, str], LLM] = {}


def for_task(task: str = "default") -> LLM:
    return LLM.for_task(task)


def llm(task: str = "default") -> LLM:
    """兼容旧调用点；传入 task 即按路由取模型。"""
    return for_task(task)
