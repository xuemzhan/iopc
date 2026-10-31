"""OPC 配置加载：TOML + .env，全局唯一入口。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "opc.toml"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


class Config:
    """点号访问的配置树，例如 cfg.get("tools.imeeting.base_url")。"""

    def __init__(self, data: dict[str, Any]):
        self._d = data

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._d
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, dotted: str) -> dict[str, Any]:
        v = self.get(dotted, {})
        return v if isinstance(v, dict) else {}

    @property
    def root(self) -> Path:
        return ROOT

    @property
    def data_dir(self) -> Path:
        # 环境变量优先：便于用临时库跑测试，不污染真实 data/
        raw = os.environ.get("OPC_DATA_DIR") or self.get("data.dir", str(ROOT / "data"))
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "opc.db"

    @property
    def vault_path(self) -> Path:
        raw = os.environ.get("OPC_VAULT_PATH") or self.get("vault.path", "")
        if not raw:
            raise RuntimeError("vault.path 未配置")
        return Path(os.path.expandvars(raw)).expanduser()

    def api_key(self) -> str:
        env = self.get("llm.api_key_env", "DEEPSEEK_API_KEY")
        key = os.environ.get(env, "")
        if not key:
            raise RuntimeError(f"缺少 LLM 密钥：请设置环境变量 {env}（写入 E:\\opc\\.env）")
        return key

    def tool(self, name: str) -> dict[str, Any]:
        return self.section(f"tools.{name}")

    def raw(self) -> dict[str, Any]:
        return self._d


def load_config(path: Path | None = None) -> Config:
    _load_dotenv()
    p = path or CONFIG_PATH
    if not p.exists():
        # 首次运行 / 测试：允许直接读示例配置
        example = p.with_name(p.name + ".example")
        if example.exists():
            p = example
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    return Config(data)


_cfg: Config | None = None


def cfg() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def reset_config() -> None:
    """丢掉缓存，下次 cfg() 重新读文件（UI 改开关后立即生效）。"""
    global _cfg
    _cfg = None
