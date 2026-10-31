"""语音入口：录一段话 → 本地 iMeeting 的 FunASR 转写 → 入库提炼。

复用 iMeeting 已经加载好的流式模型（`ws://127.0.0.1:8000/ws/stream`），
不额外起 ASR 服务、不额外占显存；协议见 iMeeting `api/ws/stream.py`：
    {"type":"start","title":...}  →  二进制 16kHz float32 PCM  →  {"type":"stop"}
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .capture import capture
from .config import cfg
from .util import sha


def _ws_url() -> str:
    return cfg().get("voice.ws_url", "ws://127.0.0.1:8000/ws/stream")


def to_pcm(path: str | Path) -> bytes:
    """任意音频 → 16kHz 单声道 float32 PCM。"""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ar", "16000", "-ac", "1", "-"],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=120,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg 转码失败：{proc.stderr.decode('utf-8', 'ignore')[:300]}")
    return proc.stdout


async def _stream(pcm: bytes, title: str, timeout_s: int = 120) -> tuple[str, int | None]:
    import websockets

    finals: list[str] = []
    meeting_id: int | None = None
    async with websockets.connect(_ws_url(), max_size=None, open_timeout=8) as ws:
        await ws.send(json.dumps({"type": "start", "title": title}, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(ws.recv(), 30)
            if not isinstance(raw, str):
                continue
            data = json.loads(raw)
            if data.get("type") == "ready":
                meeting_id = data.get("meeting_id")
                break
            if data.get("type") == "error":
                raise RuntimeError(f"ASR 拒绝会话：{data.get('message') or data.get('code')}")

        step = 3200 * 4  # 0.2 秒（16kHz × float32）
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i:i + step])
            await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "stop"}))

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), 20)
            except asyncio.TimeoutError:
                break
            if not isinstance(raw, str):
                continue
            data = json.loads(raw)
            kind = data.get("type")
            if kind == "final":
                text = (data.get("text") or "").strip()
                if text:
                    finals.append(text)
            elif kind == "error":
                raise RuntimeError(f"ASR 错误：{data.get('message') or data.get('code')}")
            elif kind == "closed":
                break
    return "".join(finals).strip(), meeting_id


def transcribe(path: str | Path, title: str | None = None) -> dict[str, Any]:
    title = title or f"语音速记 {datetime.now():%m-%d %H:%M}"
    pcm = to_pcm(path)
    if len(pcm) < 1600 * 4:  # < 0.1 秒
        raise ValueError("音频太短")
    text, meeting_id = asyncio.run(_stream(pcm, title))
    return {"text": text, "meeting_id": meeting_id, "seconds": len(pcm) / 4 / 16000}


def transcribe_and_capture(audio: str, *, on_progress=None, publish: bool | None = None) -> dict[str, Any]:
    def prog(p: int, s: str) -> None:
        if on_progress:
            on_progress(p, s)

    prog(8, "转码音频")
    result = transcribe(audio)
    text = result["text"]
    if not text:
        raise ValueError("没有识别到语音内容（可能是静音或麦克风未开）")
    prog(55, "转写完成，提炼入库")
    title = f"语音速记 {datetime.now():%m-%d %H:%M}"
    res = capture(
        kind="voice",
        external_id=f"voice:{result['meeting_id'] or sha(text)}",
        title=title,
        text=text,
        uri=f"{cfg().get('tools.imeeting.base_url', 'http://127.0.0.1:8000')}/api/v1/meetings/{result['meeting_id']}"
        if result["meeting_id"] else None,
        meta={"source": "voice", "seconds": round(result["seconds"], 1),
              "meeting_id": result["meeting_id"]},
        publish=publish,
        on_progress=lambda p, s: prog(55 + int(p * 0.45), s),
    )
    res["transcript"] = text
    res["seconds"] = round(result["seconds"], 1)
    return res


def record(seconds: int = 8, device: str | None = None) -> str:
    """用 ffmpeg 直接录一段麦克风（CLI 用）。返回临时 wav 路径。"""
    dev = device or cfg().get("voice.mic", "")
    if not dev:
        raise RuntimeError("未配置麦克风设备：先在 config/opc.toml 的 [voice].mic 填 ffmpeg 设备名")
    out = Path(tempfile.gettempdir()) / f"opc-voice-{int(time.time())}.wav"
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "dshow", "-i", f"audio={dev}", "-t", str(seconds),
         "-ar", "16000", "-ac", "1", str(out)],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=seconds + 30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"录音失败：{proc.stderr.decode('utf-8', 'ignore')[:300]}")
    return str(out)
