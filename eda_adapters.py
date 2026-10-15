"""EDA 域适配器（M3 第0段）：按 `tool in {vivado,klayout,verdi,...}` 注入对应 skill 文本。

刻意与 `engine_adapters.py` 同形：
- 同样从 `.trae/skills/eda-adapters/SKILL.md` 读，按工具切段 + 带一段 `Shared workflow`；
- 同样在 SKILL.md 缺失时**优雅返回空串**（OSError 吞掉），让上层照常跑、只是少一段提示——
  skill 文本是"锦上添花"的用法引导，不是硬依赖。
这样加新 EDA 工具 = 往 SKILL.md 加一段 + 在这里登记别名，下游解析（`agent_runtime/eda_tcl.py`）不动。
"""
from pathlib import Path

SKILL_PATH = Path(__file__).parent / ".trae" / "skills" / "eda-adapters" / "SKILL.md"

# 工具别名 → SKILL.md 里的段标题（`## <key>`）。
_TOOL_KEY = {
    "vivado": "Vivado",
    "klayout": "KLayout",
    "verdi": "Verdi",
    "vcs": "Verdi",
    "quartus": "Quartus",
    "innovus": "Innovus",
}


def skill_for_tool(tool: str = "vivado") -> str:
    """返回该 EDA 工具的适配器 skill 文本，供注入本地 agent；未知工具回落到 Vivado 段。"""
    try:
        text = SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    key = _TOOL_KEY.get(str(tool).lower(), "Vivado")
    sections = text.split("\n## ")
    # 首段（标题前的总述）始终带上。
    wanted = [text.split("\n## ", 1)[0]]
    for section in sections[1:]:
        if section.startswith(key):
            wanted.append("## " + section)
        if section.startswith("Shared workflow"):
            wanted.append("## " + section)
    return "\n".join(wanted) + f"\n\n当前项目 EDA 工具：{tool}"
