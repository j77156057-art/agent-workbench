"""store 真实语义分层与亮度系数（**产品口径，待用户确认**）。

2026-11-04 弃用 L0–L4 抽象编号（旧编号仅存在于历史痕迹，见各模块注释）；
这里按 store 的真实语义给出 `layer_key` / `layer_label`：

    rule    常驻守则         → 图的中心 hub（不进环，最亮）
    shared  共享记忆         → promote.shared_items
    legacy  项目长期记忆     → agent_memory.audit
    project 晋升候选         → promote.candidates
    hook    会话钩子         → hooks.items

**亮度顺序依据 = 内容稳定度，不再是“越底层越亮”**：
常驻守则（最稳定）> 共享记忆 > 项目长期记忆 > 晋升候选 > 会话钩子（最易变）。
这是产品口径，尚未走评审；本模块是唯一配置点，改这里即可生效，
`inspector._node` 与 `inspector.view` 不再写 if/else。

旧编号 → 新 `layer_key` 的对应关系（仅用于兼容历史 trace 快照，不再对外输出）：
L0→rule / L1→legacy / L2→shared / L3→project / L4→hook。
"""
from __future__ import annotations

LAYER_ORDER = ("rule", "shared", "legacy", "project", "hook")

LAYER_LABELS = {
    "rule": "常驻守则",
    "shared": "共享记忆",
    "legacy": "项目长期记忆",
    "project": "晋升候选",
    "hook": "会话钩子",
}

# store → layer。新增 store 必须在这里登记，否则落到 fallback（并会被接口标出来）。
STORE_LAYER = {
    "system": "rule",
    "shared": "shared",
    "legacy": "legacy",
    "project": "project",
    "hook": "hook",
}

# 亮度系数（0–1，前端按 `opacity` 落到真实像素），按内容稳定度递减。
LAYER_BRIGHTNESS = {
    "rule": 1.00,
    "shared": 0.80,
    "legacy": 0.64,
    "project": 0.46,
    "hook": 0.30,
}

# 注入 = 在层级基线上提亮一档：opacity = min(1, layerWeight * boost)。
INJECTED_BOOST = 1.22

# 不同层之间还叠加一点点深度衰减（环越外越暗），系在 memoryGraphTokens 里。
FALLBACK_LAYER = "project"

CALIBRATED = False
BRIGHTNESS_BASIS = (
    "内容稳定度：常驻守则（最稳定）> 共享记忆 > 项目长期记忆 > 晋升候选 > 会话钩子（最易变）"
)
PENDING_CONFIRMATION = "亮度顺序依据：内容稳定度（产品口径，待确认）"

# 旧编号只用于读取历史 trace/快照；新写入一律使用 layer_key。
LEGACY_LAYER_ALIASES = {"L0": "rule", "L1": "legacy", "L2": "shared", "L3": "project", "L4": "hook"}


def normalize_layer(value) -> str:
    """把历史 trace 的旧编号折成 `layer_key`；未知值原样返回，由调用方兜底。"""
    text = str(value or "")
    return LEGACY_LAYER_ALIASES.get(text, text)


def layer_for_store(store) -> str:
    """未知 store 落到 fallback，并在接口里用 `layer_fallback` 标注。"""
    return STORE_LAYER.get(str(store or ""), FALLBACK_LAYER)


def label_for(layer) -> str:
    key = normalize_layer(layer)
    return LAYER_LABELS.get(key, str(layer or ""))


def config() -> dict:
    return {
        "order": list(LAYER_ORDER),
        "labels": dict(LAYER_LABELS),
        "store_map": dict(STORE_LAYER),
        "brightness": dict(LAYER_BRIGHTNESS),
        "brightness_basis": BRIGHTNESS_BASIS,
        "injected_boost": INJECTED_BOOST,
        "fallback_layer": FALLBACK_LAYER,
        "calibrated": CALIBRATED,
        "pending_confirmation": PENDING_CONFIRMATION,
        "misfits": [],
    }
