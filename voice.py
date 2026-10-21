"""Optional speech bridge for the harness.

The browser path is the zero setup fallback. When a compatible STT/TTS endpoint
is configured, these helpers proxy audio without making the main Agent depend on
one vendor SDK.
"""
from __future__ import annotations

import json
import os
import urllib.request
import uuid


def _cfg(prefix: str):
    return (
        os.getenv(f"DOCMIND_{prefix}_URL", "").strip(),
        os.getenv(f"DOCMIND_{prefix}_API_KEY", "").strip(),
        os.getenv(f"DOCMIND_{prefix}_MODEL", "").strip(),
    )


def _endpoint(url: str, suffix: str) -> str:
    """允许用户填 base URL（到 /v1 一级），自动补成音频接口。

    这是「换模型/换服务商」最常见的填错点：OpenAI 文档给的是完整
    ``/audio/transcriptions``，而很多兼容服务只暴露 ``/v1``。两种都接受，
    完整端点原样使用。
    """
    value = (url or "").strip()
    if not value:
        return ""
    base = value.rstrip("/")
    if base.endswith("/audio/transcriptions") or base.endswith("/audio/speech"):
        return base
    if base.endswith("/v1") or base.endswith("/compatible-mode/v1"):
        return base + suffix
    return base


def configured(kind: str, url: str = "") -> bool:
    """是否配置了可用的语音端点；显式 url 优先于环境变量。"""
    if (url or "").strip():
        return True
    env_url, _key, _model = _cfg(kind.upper())
    # 本地 Ollama/Whisper/TTS 兼容服务通常不需要 API Key；云端服务再由
    # 调用方通过 DOCMIND_*_API_KEY 提供 Bearer 凭证。
    return bool(env_url)


def transcribe(audio: bytes, filename: str = "audio.webm", language: str = "",
               *, url: str = "", key: str = "", model: str = "") -> dict:
    env_url, env_key, env_model = _cfg("STT")
    url = _endpoint(url or env_url, "/audio/transcriptions")
    key = (key or env_key).strip()
    model = (model or env_model or "whisper-1").strip()
    if not url:
        return {"ok": False, "fallback": "browser", "error": "未配置服务端语音识别模型，可使用浏览器语音输入。"}
    boundary = "----DocMindVoice" + uuid.uuid4().hex
    fields = [("model", model or "whisper-1"), ("language", language)]
    body = bytearray()
    for name, value in fields:
        if value:
            body.extend((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode())
    body.extend((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: audio/webm\r\n\r\n").encode())
    body.extend(audio)
    body.extend((f"\r\n--{boundary}--\r\n").encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=bytes(body), method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    return {"ok": True, "text": str(payload.get("text") or payload.get("transcript") or "")}


def speech_request(text: str, voice: str = "", *,
                   url: str = "", key: str = "", model: str = "") -> tuple[bytes, str]:
    env_url, env_key, env_model = _cfg("TTS")
    url = _endpoint(url or env_url, "/audio/speech")
    key = (key or env_key).strip()
    model = (model or env_model or "tts-1").strip()
    voice = (voice or os.getenv("DOCMIND_TTS_VOICE", "") or "alloy").strip()
    if not url:
        raise RuntimeError("未配置服务端语音合成模型")
    payload = json.dumps({"model": model or "tts-1", "input": text,
                          "voice": voice,
                          "response_format": "mp3"}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(), response.headers.get_content_type() or "audio/mpeg"
