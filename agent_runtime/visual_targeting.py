"""Validate model-proposed desktop targets and compare a later window frame."""
from __future__ import annotations

import json
import logging
import math
import re

from PIL import Image

logger = logging.getLogger(__name__)

#: 模型给 bbox 的两种坐标系：0–1 归一化，或 0–1000 的像素尺度。
#: 判别规则必须无歧义、且宁严勿猜：
#:   1) 四个数都 ≤1 → `normalized`；
#:   2) 四个数都 >1 且都 ≤1000 → `pixels_1000`（0–1 空间不可能出现 >1）；
#:   3) 其余（含 0 与 >1 混用、越界、负值）一律解析失败，绝不取中点硬点。
#: 依据同时写进日志，审计时能看出这次是怎么判的。
PIXELS_1000_SCALE = 1000.0


def _parse_json_object(raw):
    text = str(raw or "").strip()[:5000]
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None, "返回内容不是合法 JSON"
    if not isinstance(value, dict):
        return None, "返回内容不是 JSON 对象"
    return value, ""


def _classify_bbox_space(values):
    """按数值判别 bbox 坐标系，返回 (space, basis, error)。"""
    if all(0 <= item <= 1 for item in values):
        return "normalized", "四个数都在 [0,1] 内", ""
    if all(item > 1 for item in values):
        if all(0 <= item <= PIXELS_1000_SCALE for item in values):
            basis = ("四个数都 >1 且都 ≤1000"
                     if not all(item >= 100 for item in values)
                     else "四个数都 ≥100 且都 ≤1000")
            return "pixels_1000", basis, ""
        return None, "", "bbox 数值越界：只接受 0–1 或 0–1000 两种尺度"
    return None, "", ("bbox 坐标系混合：部分数值 ≤1、部分 >1，无法判定是 0–1 还是 "
                      "0–1000，拒绝猜测位置")


def parse_visual_target_detailed(raw):
    """解析视觉模型的目标 JSON，返回 (target, error)。

    `target` 里的 `bbox` 一律是 0–1 归一化坐标，另带 `coordinate_space`
    与 `raw_bbox` 供审计。解析失败时 `error` 是可读中文原因。
    """
    value, error = _parse_json_object(raw)
    if error:
        logger.info("visual target rejected: %s", error)
        return None, error
    if value.get("found") is not True:
        return None, "模型没有声明 found=true"
    box = value.get("bbox")
    if not isinstance(box, list) or len(box) != 4:
        return None, "bbox 必须是四个数 [x1,y1,x2,y2]"
    try:
        values = [float(item) for item in box]
    except (TypeError, ValueError):
        return None, "bbox 含有非数字项"
    if not all(math.isfinite(item) for item in values):
        return None, "bbox 含有 NaN 或无穷值"
    if any(item < 0 or item > PIXELS_1000_SCALE for item in values):
        return None, "bbox 数值越界：只接受 0–1 或 0–1000 两种尺度"
    space, basis, error = _classify_bbox_space(values)
    if space is None:
        return None, error
    scale = 1.0 if space == "normalized" else PIXELS_1000_SCALE
    x1, y1, x2, y2 = (item / scale for item in values)
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        return None, ("bbox 顺序或尺寸非法：要求 x1<x2、y1<y2，且宽高不能为 0"
                      "（上下左右颠倒也不接受）")
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError):
        return None, "confidence 缺失或不是数字"
    if not math.isfinite(confidence) or not 0.8 <= confidence <= 1:
        return None, "confidence 不在 0.8–1 之间"
    label = re.sub(r"\s+", " ", str(value.get("label") or "")).strip()[:100]
    evidence = re.sub(r"\s+", " ", str(value.get("evidence") or "")).strip()[:240]
    if not label or not evidence:
        return None, "缺少 label 或画面 evidence"
    logger.info("visual target bbox space=%s basis=%s raw=%s normalized=[%.4f, %.4f, %.4f, %.4f]",
                space, basis, values, x1, y1, x2, y2)
    return {"bbox": [x1, y1, x2, y2], "raw_bbox": list(box),
            "coordinate_space": space, "label": label,
            "evidence": evidence, "confidence": round(confidence, 2)}, ""


def parse_visual_target(raw):
    target, _error = parse_visual_target_detailed(raw)
    return target


def parse_visual_verification(raw):
    """Accept only an evidenced before/after judgement; suggestions are not actions."""
    text = str(raw or "").strip()[:5000]
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("status") not in {"met", "unmet", "uncertain"}:
        return None
    status = value["status"]
    evidence = re.sub(r"\s+", " ", str(value.get("evidence") or "")).strip()[:400]
    if not evidence:
        return None
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return None
    if status != "uncertain" and confidence < 0.8:
        status = "uncertain"
    next_target = re.sub(r"\s+", " ", str(value.get("next_target") or "")).strip()[:160]
    return {"status": status, "evidence": evidence,
            "confidence": round(confidence, 2),
            "next_target": next_target if status == "unmet" else ""}


def target_fingerprint(raw, width, height, bbox):
    """Downsample a padded target neighborhood from a BGRA capture."""
    image = Image.frombytes("RGB", (int(width), int(height)), bytes(raw), "raw", "BGRX")
    x1, y1, x2, y2 = bbox
    pad_x = max(0.03, (x2 - x1) * 0.5)
    pad_y = max(0.03, (y2 - y1) * 0.5)
    left = max(0, int((x1 - pad_x) * width))
    top = max(0, int((y1 - pad_y) * height))
    right = min(width, max(left + 1, int((x2 + pad_x) * width)))
    bottom = min(height, max(top + 1, int((y2 + pad_y) * height)))
    return image.crop((left, top, right, bottom)).resize((24, 24), Image.Resampling.BILINEAR).tobytes()


def target_still_matches(before, after, *, max_mean_delta=18):
    if not before or len(before) != len(after):
        return False
    return sum(abs(a - b) for a, b in zip(before, after)) / len(before) <= max_mean_delta
