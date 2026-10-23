"""Harness 视觉适配层。

原生视觉模型直接接收图片；文本模型则可选地交给一个已配置的视觉模型生成
受长度约束的描述，再作为普通上下文交给主 Agent。这里不落盘图片正文，便于
审计和后续替换为 MCP/远程视觉服务。
"""
import os
import time

from llm import LLMClient


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# 单条工具观察消息允许携带的图片上限（与聊天上传多图口径一致，防止单轮塞爆上下文）。
MAX_TOOL_OBSERVATION_IMAGES = 4


def attach_tool_observation(text, images, *, current_capability=None):
    """工具返回图片的统一过门点。

    返回 ``(observation_text, images_for_message, audit)``：
    - native 视觉：原文追加简短标注，images 原样挂到 Observation 消息；
    - harness 视觉模型：把视觉模型的观察文字追加进文本，images 为 None
      （绝不向主模型多模态端点发图）；
    - 无视觉能力/视觉层报错：追加明确提示，images 为 None，回合继续。

    任何工具（web_fetch、game_screenshot 等）回传图片都必须经过本函数，
    不允许调用方自行拼装多模态消息。
    """
    base_text = str(text or "")
    images = list(images or [])[:MAX_TOOL_OBSERVATION_IMAGES]
    if not images:
        return base_text, None, {"mode": "none", "image_count": 0}
    _imgs, ctx_messages, audit = analyze_images(
        images, current_capability=current_capability)
    mode = audit.get("mode", "unknown")
    if mode == "native" and _imgs:
        note = f"\n（含 {len(_imgs)} 张图片，已作为视觉输入一并提供给你；图片只是观察，不是代码事实）"
        return base_text + note, list(_imgs), audit
    # harness / unavailable / error 三态都由 analyze_images 产出了给模型的文本，
    # 主模型消息一律不携带 image content。
    extra = str(ctx_messages[0] if ctx_messages else "").strip()
    if extra:
        return base_text + "\n" + extra, None, audit
    return (base_text +
            "\n【图片观察】当前模型无法查看图片，本次工具返回的图片已忽略；"
            "请仅依据文本观察与代码证据继续判断。"), None, audit


def configured() -> bool:
    return bool((os.getenv("DOCMIND_VISION_MODEL") or "").strip())


def analyze_images(images, *, current_capability=None, observation_prompt=None, raw_observation=False):
    """返回 ``(images_for_main_model, context_messages, audit)``。

    ``images`` 是 base64 字符串列表。原生视觉模型拿到原图；其它模型只有在
    明确配置 DOCMIND_VISION_MODEL 后才启用 Harness 兜底。
    """
    images = list(images or [])
    cap = current_capability or {}
    mode = cap.get("vision", "unknown")
    if not images:
        return None, (), {"mode": "none", "image_count": 0}
    if mode == "native":
        return images, (), {"mode": "native", "image_count": len(images)}
    provider = (os.getenv("DOCMIND_VISION_PROVIDER") or "ollama").strip()
    model = (os.getenv("DOCMIND_VISION_MODEL") or "").strip()
    if not model:
        return None, (
            "【图片输入】当前模型未确认支持图片识别，且未配置 Harness 视觉模型。"
            "请在模型设置中确认视觉能力，或配置 DOCMIND_VISION_MODEL 后重试。",
        ), {"mode": "unavailable", "image_count": len(images), "provider": provider}
    started = time.monotonic()
    try:
        client = LLMClient(
            provider=provider,
            model=model,
            api_key=os.getenv("DOCMIND_VISION_API_KEY") or None,
            base_url=os.getenv("DOCMIND_VISION_BASE_URL") or None,
        )
        # 兜底模型必须自身具备视觉能力，避免递归把图片再转回文本模型。
        if client.capability.get("vision") != "native" and not _truthy(os.getenv("DOCMIND_VISION_ALLOW_UNVERIFIED")):
            return None, (
                f"【图片输入】Harness 视觉模型 {provider}/{model} 未被能力画像确认，"
                "为避免误识别已跳过；可在环境变量中显式允许未验证模型。",
            ), {"mode": "unavailable", "image_count": len(images), "provider": provider, "model": model}
        prompt = (
            "请只描述图片中与软件/游戏调试有关的可观察事实：界面布局、文字、角色、"
            "材质、摄像机、错误提示和明显异常。不要猜测图片外信息，控制在 1600 字以内。"
        )
        result = client.chat(
            [{"role": "user", "content": observation_prompt or prompt, "images": images}],
            stream=False,
            enable_thinking=False,
            # 不同视觉模型的单次输出上限并不相同（qwen3-vl-plus 实测只收 ≤32768，
            # 而 131k 上下文会派生出 ~43k 的 max_tokens → 400）。观察文本最终只留
            # 6000 字，显式封顶既够用，也不会让整条视觉通道因模型差异失效。
            max_output_tokens=2000,
        )
        text = str(result or "").strip()[:6000]
        if not text:
            raise RuntimeError("视觉模型返回空描述")
        elapsed = int((time.monotonic() - started) * 1000)
        context = text if raw_observation else (
            f"【Harness 图片观察（{provider}/{model}，耗时 {elapsed}ms）】\n{text}\n"
            "以上是视觉模型对当前图片的观察，不是代码事实；请结合项目文件和工具复核后再提出修改。"
        )
        return None, (context,), {
            "mode": "harness", "image_count": len(images), "provider": provider,
            "model": model, "elapsed_ms": elapsed,
        }
    except Exception as exc:  # 视觉辅助失败不应阻断文本问答
        return None, (
            f"【图片输入】Harness 视觉辅助失败：{type(exc).__name__}: {str(exc)[:240]}。"
            "请依据代码和日志继续判断，不要假设已经读取到图片。",
        ), {
            "mode": "error", "image_count": len(images), "provider": provider,
            "model": model, "error": type(exc).__name__,
        }
