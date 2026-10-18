"""DocMind 配置中心：从 .env 读取，集中管理模型 / 路径 / 参数。"""
import atexit
import json
import contextvars
import os
import re
import sys
from datetime import datetime
import tempfile

from dotenv import load_dotenv

import temp_state

load_dotenv()

# 资源根目录：未打包时取项目目录；PyInstaller 单文件(onefile)解包后取 _MEIPASS；
# 目录(onedir)模式（本项目实际打包方式，PyInstaller>=5.13）把收集的资源放在 exe
# 同级的 _internal/ 下。onedir 不会设置 sys._MEIPASS，必须按实际落点定位，否则会
# 回退去读源码目录的 .chroma/web（一旦把 dist 文件夹挪走就全盘失效）。
if getattr(sys, "frozen", False):
    EXE_DIR = os.path.dirname(sys.executable)
    if hasattr(sys, "_MEIPASS"):
        BASE_DIR = sys._MEIPASS
    elif os.path.isdir(os.path.join(EXE_DIR, "_internal", "web")):
        BASE_DIR = os.path.join(EXE_DIR, "_internal")
    else:
        BASE_DIR = EXE_DIR
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 项目根目录与前端目录（供 api.py 等模块复用）
PROJECT_DIR = BASE_DIR
PROJECT_WEB_DIR = os.path.join(PROJECT_DIR, "web")

# 运行时状态根：.chroma / .docmind* / 会话 / trace / 预算等派生自此。
# 显式 DOCMIND_STATE_ROOT 优先；测试进程默认隔离到临时目录（不污染仓库）。
STATE_ROOT = os.getenv("DOCMIND_STATE_ROOT") or BASE_DIR


def _running_under_unittest():
    """当前进程是否由 `python -m unittest ...` 驱动（严格判据）。

    过去判据是 `"unittest" in sys.modules`（外加 argv[0] 猜测），会把「任何 import
    了 unittest 的进程」（如 `python -c "import unittest, config"`）也误判为测试
    进程，把状态根搬到临时目录，让用户状态「看起来丢了」。这里改用 runpy 的精确
    特征：`-m unittest` 时 `sys.modules["__main__"].__spec__.name == "unittest.__main__"`
    （unittest/__main__.py 以 __main__ 身份执行，spec 名保留）；普通 `import unittest`
    或 `python script.py` 的 __main__ 无此 spec，故只在真正的 `-m unittest` 进程命中。
    """
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return getattr(spec, "name", None) == "unittest.__main__"


def _running_under_pytest():
    """当前进程是否运行在 pytest 会话中。

    pytest 引导后其自身包必然已在 sys.modules，随后才导入 conftest / 测试模块
    （此时才 import config）；而桌面版、exe 与普通 python 运行都不会安装/导入
    pytest，故该信号可靠。不能只看 sys.argv[0]：pytest 可被 python -m pytest、
    console_script、IDE 插件等多种方式启动。
    """
    return "pytest" in sys.modules


# 最小、可解释的测试隔离：`python -m unittest` 与 pytest 进程都生效。打包 exe /
# dev 服务不满足该判据，行为不变；显式设置 DOCMIND_STATE_ROOT 优先于本默认；
# DOCMIND_NO_TEST_ISOLATION=1 可关闭（例如想跑真实状态根时）。
# 历史教训：只判 unittest 时，pytest 跑 tests/test_api_model_config.py 会把
# llm_provider=mock / llm_model=zz-unit-ctx 等夹具值 save_state 进真实状态文件，
# 用户重启后云端模型被悄悄打回离线 mock。
_TEST_STATE_ISOLATED = bool(
    not os.getenv("DOCMIND_STATE_ROOT")
    and (_running_under_unittest() or _running_under_pytest())
    and os.getenv("DOCMIND_NO_TEST_ISOLATION") != "1"
)
if _TEST_STATE_ISOLATED:
    STATE_ROOT = tempfile.mkdtemp(prefix="docmind_test_state_")
    # 每个测试进程留一个：实测攒到 1,794 个目录 / 239 MB / 1.9 万个文件（里面是 .chroma
    # 与 sqlite）。正常退出靠 atexit 自己删；被硬杀留下的由下一个进程扫掉——判据是归属
    # PID，不是目录 mtime（Windows 上往子目录写文件不推进父目录 mtime，用它误删过正在
    # 使用的浏览器 profile）。显式设了 DOCMIND_STATE_ROOT 的走不到这里，绝不会被删。
    temp_state.claim(STATE_ROOT)
    atexit.register(temp_state.release, STATE_ROOT)
    temp_state.sweep_orphans(tempfile.gettempdir(),
                             ("docmind_test_state_", "docmind_deveval_"))


def state_path(env_name, default):
    """取运行时状态路径：显式 env 优先；测试隔离生效时相对路径以 STATE_ROOT 为基准。

    - env 未设置（空串视为未设置）→ 返回 default（通常已派生自 STATE_ROOT）；
    - env 是绝对路径 → 原样返回；
    - env 是相对路径 且 测试隔离生效 → 挂到 STATE_ROOT 下（否则会相对 cwd 写进仓库根）；
    - env 是相对路径 且 未隔离 → 原样返回（保持「显式 env 优先」语义）。
    """
    raw = os.getenv(env_name)
    if not raw:
        return default
    if _TEST_STATE_ISOLATED and not os.path.isabs(raw):
        return os.path.join(STATE_ROOT, raw)
    return raw

# ---- LLM Provider ----
# 可选: qwen(通义千问) / deepseek / kimi(月之暗面) / zhipu(智谱) /
#       siliconflow(硅基流动) / openai / ollama / llamacpp /
#       custom(任意 OpenAI 兼容端点，base_url 运行时填写) / mock(离线演示)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock")

# 各 provider 的 OpenAI 兼容接入点（base_url 统一为 OpenAI 协议，与 Android 端 AIApiClient 思路一致）
# cloud=True 表示外部云端服务（请求出网、按 token 计费）；本地服务与 mock 为 False。
PROVIDERS = {
    "qwen": {
        "label": "通义千问（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "default_model": "qwen-plus",
        "cloud": True,
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "cloud": True,
    },
    "kimi": {
        "label": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "default_model": "kimi-k2-0905-preview",
        "cloud": True,
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY",
        "default_model": "glm-4.6",
        "cloud": True,
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "default_model": "Qwen/Qwen3-8B",
        "cloud": True,
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "cloud": True,
    },
    "ollama": {
        "label": "本地 Ollama",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "",
        "default_model": "qwen2.5:7b",
        "cloud": False,
    },
    "llamacpp": {
        "label": "本地 llama.cpp",
        "base_url": "http://localhost:8080/v1",
        "api_key_env": "",
        "default_model": "qwen3.6-35b-a3b",
        "cloud": False,
    },
    # 任意 OpenAI 兼容服务：base_url / model / key 全部运行时填写
    "custom": {
        "label": "自定义 OpenAI 兼容服务",
        "base_url": "",
        "api_key_env": "",
        "default_model": "",
        "cloud": True,
    },
    "mock": {
        "label": "离线演示（mock）",
        "base_url": "",
        "api_key_env": "",
        "default_model": "mock",
        "cloud": False,
    },
}

LLM_MODEL = os.getenv("LLM_MODEL", "")  # 为空则用 provider 默认
LLM_API_KEY = os.getenv("LLM_API_KEY", "")  # 直接指定可覆盖环境变量

# ---- Embedding ----
# 可选: qwen(通义千问 text-embedding-v3, 需 key) / ollama(本机 Ollama nomic-embed-text, 零 Key) / local(本地哈希占位, 仅离线演示)
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "bge-m3" if EMBEDDING_PROVIDER == "ollama" else "text-embedding-v3",
)
LOCAL_EMBED_DIM = 256  # 本地兜底向量维度（仅离线演示用，非语义向量）

# ---- 路径 / 参数 ----
# .chroma 与其余状态文件同源于 STATE_ROOT（打包后 STATE_ROOT 默认 = 资源目录）。
# 该目录**不随包分发**（见 docmind.spec，否则会泄漏开发者的本地代码索引并额外增加约
# 377MB 体积），由 chromadb 首次启动时自动创建空目录，用户通过 /api/ingest_code 索引
# 自己的代码库。CHROMA_DIR 显式 env 仍优先。
CHROMA_DIR = state_path("CHROMA_DIR", os.path.join(STATE_ROOT, ".chroma"))


def ensure_dirs():
    """幂等创建运行时目录（惰性）。

    严禁在模块导入期调用——导入不应产生任何磁盘副作用。由 api.py 的 lifespan 与
    vectorstore 建立 chroma 客户端前显式调用。
    """
    try:
        os.makedirs(CHROMA_DIR, exist_ok=True)
    except OSError:
        pass


COLLECTION_NAME = os.getenv("COLLECTION_NAME", "docmind")
CODE_COLLECTION_NAME = os.getenv("CODE_COLLECTION_NAME", "docmind_code")
# 跨会话经验记忆（Phase 3）独立集合：与文档/代码集合并列，不污染索引。
EXPERIENCE_COLLECTION_NAME = os.getenv("EXPERIENCE_COLLECTION_NAME", "docmind_experience")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "80"))
TOP_K = int(os.getenv("TOP_K", "4"))
# 0 means no fixed tool-step ceiling. Repetition, failure-streak, cancellation,
# deadline and approval guards remain active.
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "0"))
AUDIT_MAX_AGENT_STEPS = int(os.getenv("DOCMIND_AUDIT_MAX_STEPS", "0"))
CODE_MAX_AGENT_STEPS = int(os.getenv("DOCMIND_CODE_MAX_STEPS", "0"))
# 纯目录勘察（list_dir）每轮前若干次不计入工具步数：浏览目录结构是只读导航，
# 不应挤占 read_file/grep 的取证预算；超过免费额度后照常计步。
# 相同参数重复调用仍由 Agent 防重复护栏拦截，防止靠它无限空转。
NAV_FREE_STEPS = int(os.getenv("DOCMIND_NAV_FREE_STEPS", "2"))
# ---- Agent 上下文预算（防止长系统提示 + 多轮观察 + 思考模型 reasoning 撑爆 n_ctx）----
# 多轮记忆回放的问答对【硬上限】：实际回放多少轮先按 token 窗口动态决定
# （Agent._history_window，占 prompt 预算 COMPACT_KEEP_RATIO），此值只兜底防失控。
AGENT_HISTORY_TURNS = int(os.getenv("AGENT_HISTORY_TURNS", "40"))
OBS_MAX_CHARS = int(os.getenv("OBS_MAX_CHARS", "8000"))  # 只在上下文预算不足时按 token 裁剪
AUDIT_OBS_MAX_CHARS = int(os.getenv("DOCMIND_AUDIT_OBS_CHARS", os.getenv("DOCMIND_AUDIT_OBS_MAX_CHARS", "12000")))
HISTORY_ANSWER_CHARS = int(os.getenv("HISTORY_ANSWER_CHARS", "12000"))  # 兼容旧调用方；历史回放不再按字符截断
TRAIL_ASSISTANT_CHARS = int(os.getenv("TRAIL_ASSISTANT_CHARS", "8000"))  # 上下文预算不足时才丢弃更早往返
# 每轮送模型前，整段 prompt 的 token 预算（llamacpp 走 /tokenize 精算）。
# 仅作为 env 显式覆盖值与无画像客户端的兜底；实际默认值按模型真实窗口缩放
# （见 prompt_token_budget）：16k 本地模型约 12k，131k 云端模型可放到 ~98k，
# 避免「小窗口塞不下、大窗口浪费 90%」的一刀切。
PROMPT_TOKEN_BUDGET = int(os.getenv("PROMPT_TOKEN_BUDGET", "11000"))
# 历史回放/压缩保留占 prompt 预算的比例：留出其余空间给系统提示、当前问题与
# 本轮工具往返（observation）。触发压缩的历史占用比例（80%：大窗口模型尽量
# 少丢早期原文，到额度八成再压缩；保留 35% 给最近轮次）。
COMPACT_KEEP_RATIO = float(os.getenv("DOCMIND_COMPACT_KEEP_RATIO", "0.35"))
COMPACT_TRIGGER_RATIO = float(os.getenv("DOCMIND_COMPACT_TRIGGER_RATIO", "0.8"))
# 单次补全上限（含思考型模型的 reasoning）：防止模型不按格式收尾时无限生成，
# 到顶后 finish_reason=length，Agent 会保留已有正文并接续生成。
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "3072"))
# prompt 预算与输出预算之间的安全余量（token）。原为散落的字面量 512，
# 已提为常量：两处（prompt_token_budget / output_token_budget）必须用同一个值，
# 否则会出现「prompt 算出的可用量 + 输出预算 > 窗口」的静默越界。
OUTPUT_TOKEN_RESERVE = int(os.getenv("DOCMIND_OUTPUT_RESERVE", "512"))
# 是否允许思考型模型把预算花在 reasoning_content 上。ReAct 工具路由不需要长思考，
# 实测关思考后同类请求 55 token/4s 给出 Action（开思考时偶发烧满 3072 token 不行动）。
# 仅对本地 OpenAI 兼容服务（llamacpp/ollama）透传 chat_template_kwargs。
LLM_ENABLE_THINKING = os.getenv("LLM_ENABLE_THINKING", "0") == "1"

# ---- 模型能力画像 ---------------------------------------------------------
# 不同模型家族差异很大，不能一套参数打天下：
# · 本地模型需要按自身上下文窗口扩 num_ctx（窗口小的模型不能盲目给 14k prompt）；
# · 思考分两类：native（reasoner 类模型天生输出 reasoning_content，无需开关）
#   与 toggle（qwen3 家族靠 enable_thinking / think 参数切换）；
# · 其余模型不支持思考，传了参数反而可能 400，必须跳过。
# 画像按 (provider, 模型名子串) 匹配，未命中走 provider 默认值。

# (provider, 模型名小写子串) -> 上下文窗口 token 数
#
# 匹配顺序：**字典插入顺序**，首个命中即返回（见 model_context_window）。
# 因此更具体的名（deepseek-flash）必须排在更宽泛的名（deepseek）之前，
# 否则后者会先把前者吃掉。
_MODEL_CONTEXT_OVERRIDES = {
    # DeepSeek V4.1-Flash：官方 1M 上下文（一手：api-docs.deepseek.com 模型页
    # 标 1M / 2026-09 起）。此前**没有这一条**，于是落到 provider 默认 131072，
    # 只拿到真实窗口的 1/8 —— 实测 trace 里 prompt_tokens 最大已到 295,074，
    # 早已越过 131,072 的"假窗口"，说明这个默认值既不真实、也没能起到保护作用。
    # 旧名 deepseek-v4-flash(-vision-exp) 已路由到同一模型，一并覆盖。
    ("deepseek", "deepseek-flash"): 1048576,
    ("deepseek", "deepseek-v4-flash"): 1048576,
    ("deepseek", "reasoner"): 65536,
    ("openai", "gpt-4o"): 128000,
    ("openai", "gpt-4-"): 65536,
    ("openai", "gpt-3.5"): 16385,
    ("openai", "o1"): 200000,
    ("openai", "o3"): 200000,
    ("ollama", "qwen2.5:7b"): 32768,
}

# provider 级默认上下文窗口（模型未命中精确画像时使用）
_PROVIDER_CONTEXT_DEFAULT = {
    "qwen": 131072,
    "deepseek": 131072,
    "kimi": 131072,
    "zhipu": 131072,
    "siliconflow": 65536,
    "openai": 128000,
    "ollama": 16384,
    "llamacpp": 16384,
    "custom": 32768,
    "mock": 32768,
}

# 模型名命中任一子串即视为"原生思考模型"（推理始终开启，思考走 reasoning_content）
_NATIVE_THINK_HINTS = (
    "reasoner", "qwq", "-r1", "_r1", "/r1", "think", "o1-", "o3-", "o4-",
    "gpt-5", "deepseek-r1",
)

# 支持 enable_thinking 开关的家族：(provider, 模型名前缀/子串)
#
# ⚠ **这里只回答「能不能开关」，不回答「参数长什么样」** —— 形状在 `llm.py` 里按
# provider 分支。两者必须同时改：只把 provider 加进来而不改形状，会发一个
# 上游不认的参数，**上游要么 400 要么静默忽略**，结果是「开关能点了但思考没开」，
# 比原来更坏（原来至少是诚实地报「不支持」）。
_TOGGLE_THINK_RULES = (
    ("qwen", "qwen3"),       #  DashScope 兼容模式 qwen3 系列支持 extra_body 开关
    ("ollama", "qwen3"),
    ("llamacpp", "qwen3"),
    ("custom", "qwen3"),
    ("siliconflow", "qwen3"),
    # DeepSeek V4 / V4.1-Flash：**可开关**，但参数形状与 qwen 完全不同。
    # 一手依据：官方 Thinking Mode 文档（api-docs.deepseek.com/guides/thinking_mode）
    # 的示例直接用 `model="deepseek-flash"` 配
    # `extra_body={"thinking": {"type": "enabled"}}` + `reasoning_effort`，
    # 流式返回 `chunk.choices.delta.reasoning_content`；
    # Change Log（api-docs.deepseek.com/updates）记 V4-Pro/V4-Flash 支持
    # low/high/max 三档 thinking effort。
    #
    # 旧名 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 已退役并路由到
    # 同一模型，所以一并覆盖。
    ("deepseek", "deepseek-flash"),
    ("deepseek", "deepseek-v4-flash"),
)

# 视觉能力画像。未知模型不自动把图片交给原模型，除非用户在设置里确认；
# 未确认时由 Harness 视觉兜底（若已配置）。
_NATIVE_VISION_HINTS = (
    "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qwen3.6",
    "llava", "gemma-3", "gemma3", "gpt-4o", "gpt-4.1", "gpt-5",
    "claude-3", "claude-4", "glm-4v", "glm-4.5v", "kimi-vl", "deepseek-vl",
    # DeepSeek-V4.1-Flash（2026-09-10 起，model id=deepseek-flash）原生多模态，
    # 标准 OpenAI image_url 格式；旧名 deepseek-v4-flash(-vision-exp) 已路由到同一模型。
    "deepseek-flash", "deepseek-v4-flash",
)
_NATIVE_VIDEO_HINTS = ("gemini", "gpt-4o", "gpt-4.1", "gpt-5", "qwen3-vl")


def model_context_window(provider: str, model: str, live_window: int = None) -> int:
    """该模型可用的上下文窗口（token）。

    优先级：用户自定义覆盖（模型设置里手填，跨重启持久化）> 实时探测值
    （如 Ollama /api/show 返回的 context_length）> 内置模型画像 > 厂商默认。
    """
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    custom = get_context_window_override(p, m)
    if custom:
        return custom
    if live_window:
        return int(live_window)
    for (pp, hint), win in _MODEL_CONTEXT_OVERRIDES.items():
        if p == pp and hint in m:
            return win
    return _PROVIDER_CONTEXT_DEFAULT.get(p, 32768)


def prompt_token_budget(provider: str, model: str, context_window: int = None) -> int:
    """单次请求 prompt 可用的 token 预算，按模型真实窗口缩放。

    - env PROMPT_TOKEN_BUDGET 显式设置时强制采用（排障/压测用）；
    - context_window 可传入实时探测/自定义的窗口值（缺省走 model_context_window）；
    - 默认取「窗口的 75%」与「窗口 - 输出上限 - 余量」的较小值，
      并保 6144 下限（但受窗口可用量约束）。16k 本地模型 ≈ 12k，131k 云端 ≈ 98k，
      1M 模型 ≈ 786k；窗口过小时退化为窗口的一半（至少 256），绝不越过窗口。
    """
    env = os.getenv("PROMPT_TOKEN_BUDGET", "").strip()
    if env:
        try:
            return max(1024, int(env))
        except ValueError:
            pass
    win = int(context_window) if context_window else model_context_window(provider, model)
    # 预留量：取「输出上限」与 OUTPUT_TOKEN_RESERVE 的较大者。
    # 原来写死 LLM_MAX_TOKENS(3072)+512，与真实输出预算（可到 346k）脱节——
    # 大窗口模型会把 75% 全给 prompt，输出侧实际没有余量。
    reserve = max(LLM_MAX_TOKENS, output_token_budget(provider, model, win)) \
        + OUTPUT_TOKEN_RESERVE
    usable = win - reserve
    if usable <= 0:
        # 窗口连输出预留都放不下：退化为窗口的一半（至少 256）
        budget = max(256, int(win * 0.5))
    else:
        scaled = int(win * 0.75)
        budget = min(usable, scaled)
        budget = max(min(6144, usable), budget)   # 保 6144 下限，但不得越过 usable
        budget = max(256, budget)
    return budget


# 单次补全（输出）上限：由模型上下文窗口派生，而非写死 3072。
# 动机：云端大模型（DeepSeek 等）上下文常达 128k~1M，3072 的硬上限纯属"畏手畏脚"；
# 本地小窗口模型则保底 LLM_MAX_TOKENS，避免单次生成撑爆显存/时延。
OUTPUT_TOKEN_FRACTION = float(os.getenv("DOCMIND_OUTPUT_FRACTION", "0.33"))
OUTPUT_TOKEN_MIN = int(os.getenv("DOCMIND_OUTPUT_MIN_TOKENS", str(LLM_MAX_TOKENS)))   # 默认保底 3072
# 单次输出上限。**0 = 不设全局硬顶**（2026-10 起），只受「窗口 * FRACTION」约束。
#
# 此前写死 65536，对 1M 窗口的 deepseek-flash 意味着**只给到真实能力的 6%**：
#   1M * 0.33 = 346,030  →  被钳到 65,536
# 用户报告「模型有 100 万上下文，系统只给 4.3 万输出」正是这条 + 窗口画像缺条目
# （见 _MODEL_CONTEXT_OVERRIDES）叠加的结果：4.3 万 = 131072*0.33。
# 两个都修掉后，deepseek-flash → 346,030；想再压回一个硬顶就把本值设成正数。
OUTPUT_TOKEN_MAX = int(os.getenv("DOCMIND_OUTPUT_MAX_TOKENS", "0"))


def output_token_budget(provider: str, model: str, context_window: int = None) -> int:
    """单次补全可输出的最大 token，按模型真实窗口派生。

    - 环境变量 LLM_MAX_TOKENS 显式设置时强制采用（排障/压测用，等价于旧硬上限）；
    - 否则取「窗口 * OUTPUT_TOKEN_FRACTION」，下限 OUTPUT_TOKEN_MIN，
      上限 OUTPUT_TOKEN_MAX（**0 = 不限**），且不超过窗口本身（减 OUTPUT_TOKEN_RESERVE 余量）。
      1M 窗口 → ≈346k；131k → ≈43k；32k 本地 → ≈10k（仍≥保底 3k）。
    """
    env = os.getenv("LLM_MAX_TOKENS", "").strip()
    if env:
        try:
            return max(256, int(env))
        except ValueError:
            pass
    win = int(context_window) if context_window else model_context_window(provider, model)
    out = int(win * OUTPUT_TOKEN_FRACTION)
    out = max(OUTPUT_TOKEN_MIN, out)
    if OUTPUT_TOKEN_MAX > 0:          # 0 = 不设硬顶，交给窗口本身约束
        out = min(OUTPUT_TOKEN_MAX, out)
    out = min(out, max(256, win - OUTPUT_TOKEN_RESERVE))
    return out


def model_thinking_mode(provider: str, model: str) -> str:
    """思考能力画像：'native'（天生推理）/ 'toggle'（可开关）/ 'none'（不支持）。"""
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    if any(h in m for h in _NATIVE_THINK_HINTS):
        return "native"
    for pp, hint in _TOGGLE_THINK_RULES:
        if p == pp and hint in m:
            return "toggle"
    return "none"


def model_vision_mode(provider: str, model: str) -> str:
    """图片能力：native=原生支持，unknown=未确认，none=明确不支持。"""
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    if any(h in m for h in _NATIVE_VISION_HINTS):
        return "native"
    if p in ("mock", "deepseek", "kimi", "zhipu"):
        return "unknown"
    return "unknown"


def model_video_mode(provider: str, model: str) -> str:
    """视频能力：native=原生视频，frames=Harness 抽帧，unknown=未确认。"""
    m = (model or "").strip().lower()
    if any(h in m for h in _NATIVE_VIDEO_HINTS):
        return "native"
    return "unknown"


def model_capability(provider: str, model: str, context_window: int = None) -> dict:
    """汇总模型能力：{context_window, thinking, vision, video, cloud}。

    context_window 可传入实时探测值（如 Ollama /api/show）；用户自定义覆盖优先。
    """
    cfg = PROVIDERS.get(provider or "", {})
    cap = {
        "context_window": model_context_window(provider, model, live_window=context_window),
        "thinking": model_thinking_mode(provider, model),
        "vision": model_vision_mode(provider, model),
        "video": model_video_mode(provider, model),
        "cloud": bool(cfg.get("cloud")),
    }
    override = get_model_capability_override(provider, model)
    if override:
        cap.update({k: v for k, v in override.items() if k in ("thinking", "vision", "video")})
    return cap


# 模型上下文窗口文档检索缓存（避免每次请求都联网）
_MODEL_CTX_CACHE_FILE = os.path.join(STATE_ROOT, ".docmind_model_context.json")
_MODEL_CTX_CACHE = {}


def _load_model_ctx_cache():
    if not _MODEL_CTX_CACHE:
        try:
            if os.path.isfile(_MODEL_CTX_CACHE_FILE):
                with open(_MODEL_CTX_CACHE_FILE, "r", encoding="utf-8") as f:
                    _MODEL_CTX_CACHE.update(json.load(f))
        except (OSError, ValueError):
            pass
    return _MODEL_CTX_CACHE


def _save_model_ctx_cache(cache):
    try:
        with open(_MODEL_CTX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass


def _parse_context_from_text(text):
    """从文档正文抽取上下文窗口 token 数；抽不到返回 None。

    匹配数字（可选 K/M 后缀）前后 40 字符内出现 context/window/token 关键词，
    两种词序都支持（"context length 32768" / "128K context" / "supports 1M tokens"）。
    """
    if not text:
        return None
    # web_search 等检索器可能返回带 .text 的对象（而非裸字符串），统一落地为 str
    if not isinstance(text, str):
        text = getattr(text, "text", "") or ""
    lowered = text.lower()
    best = None
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([KkMm]?)", text):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        unit = m.group(2).lower()
        tok = int(val * (1_000 if unit == "k" else 1_000_000)) if unit else int(val)
        if not (2048 <= tok <= 10_000_000):
            continue
        start, end = m.span()
        around = lowered[max(0, start - 40):end + 40]
        if any(k in around for k in ("context", "window", "token")):
            best = tok if best is None else max(best, tok)
    return best


def resolve_context_from_docs(provider, model, web_fn, cache=None):
    """联网检索官方文档判断模型最大上下文长度；命中则返回 token 数，否则 None。

    web_fn(query) -> str（搜索/抓取到的文本，可能为空）。结果按 (provider,model)
    缓存到 .docmind_model_context.json，避免重复联网。仅在静态画像未精确到具体模型时调用。
    """
    if not callable(web_fn):
        return None
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    cache = cache if cache is not None else _load_model_ctx_cache()
    key = f"{p}:{m}"
    if key in cache:
        return cache[key] or None
    try:
        text = web_fn(f"{model} official maximum context window length tokens") or ""
    except Exception:
        text = ""
    win = _parse_context_from_text(text)
    try:
        cache[key] = win
        _save_model_ctx_cache(cache)
    except OSError:
        pass
    return win

CODE_ROOT = os.getenv("CODE_ROOT", "")  # 代码问答模式的代码库根目录；为空表示未配置
CODE_CHUNK = int(os.getenv("CODE_CHUNK", "1200"))  # 单个代码切片的最大字符数
EDIT_CONFIRM = os.getenv("EDIT_CONFIRM", "0") == "1"  # 写工具是否需要人工确认（改前出 diff）
# python_exec 分级同意护栏：开=按三档分类（根内读写增改免批 / 删除或越界=弹窗确认 /
# 不可逆=红三连确认，凭据·系统·格式化=永不放行硬底线）；关=退回旧的"越界/凭据一律硬拦"。
# 默认开；env DOCMIND_EXEC_APPROVAL=0 可一键回退到旧硬拦语义。
EXEC_APPROVAL = os.getenv("DOCMIND_EXEC_APPROVAL", "1") == "1"
# 各档需要的连续确认次数：第二档一次、第三档三次（红）。第一档 0（免批）。
EXEC_TIER2_CONFIRM = int(os.getenv("DOCMIND_EXEC_TIER2_CONFIRM", "1"))
EXEC_TIER3_CONFIRM = int(os.getenv("DOCMIND_EXEC_TIER3_CONFIRM", "3"))
API_TOKEN = os.getenv("DOCMIND_API_TOKEN", "").strip()  # 为空保持本机免鉴权

# ---- 外部接入 DocMind API 的 CORS ----
# 逗号分隔的允许来源（如 https://app.example.com,http://localhost:3000）；
# 设为 * 表示允许任意来源（默认）。仅在「外部网页/前端要调用本服务」时需要。
DOCMIND_CORS_ORIGINS = [
    o.strip() for o in os.getenv("DOCMIND_CORS_ORIGINS", "*").split(",") if o.strip()
] or ["*"]

# ---- Agent 调用外部业务 API 的白名单 ----
# 逗号分隔，支持三种写法：api.example.com（精确匹配且含其子域）、
# *.example.com（仅子域，不含裸 example.com）、*（任意 host，协议仍限 http/https）。
# 命中其一才允许发起请求，防止 Agent 被诱导对内网/元数据地址做 SSRF
# （30x 重定向的每一跳也会重新过此白名单）。为空则禁止使用 dev_http_request。
EXTERNAL_API_ALLOWLIST = [
    h.strip().lower()
    for h in os.getenv("EXTERNAL_API_ALLOWLIST", "").split(",")
    if h.strip()
]

# ---- AI 越界访问模式：safe=仅项目内；high=允许受控越界读写（须配置白名单）----
EXTERNAL_ACCESS_MODE = os.getenv("DOCMIND_ACCESS_MODE", "safe").strip().lower()
if EXTERNAL_ACCESS_MODE not in ("safe", "high"):
    EXTERNAL_ACCESS_MODE = "safe"

# ---- 网络搜索 / URL 获取服务商配置 ----
# 默认从 .env 读取；运行时可通过 /api/config 切换并持久化到 STATE_FILE。
WEB_SEARCH_PROVIDER = os.getenv("WEB_SEARCH_PROVIDER", "builtin_auto").strip().lower()
WEB_SEARCH_API_KEY = os.getenv("WEB_SEARCH_API_KEY", "")
WEB_SEARCH_API_URL = os.getenv("WEB_SEARCH_API_URL", "")
WEB_FETCH_PROVIDER = os.getenv("WEB_FETCH_PROVIDER", "builtin").strip().lower()
WEB_FETCH_API_KEY = os.getenv("WEB_FETCH_API_KEY", "")
WEB_FETCH_API_URL = os.getenv("WEB_FETCH_API_URL", "")
# True=优先使用模型内置 Web 工具（如模型本身支持联网），False=使用 DocMind 自带的 web_search/web_fetch
WEB_SEARCH_PREFER_BUILTIN = os.getenv("WEB_SEARCH_PREFER_BUILTIN", "0").strip().lower() in ("1", "true", "yes")

_WEB_SEARCH_PROVIDERS = {
    "builtin_auto", "ddg", "bing", "baidu", "exa", "tavily", "searxng",
    "zhipu", "bocha", "querit", "firecrawl", "parallel", "mcp_exa",
}
_WEB_FETCH_PROVIDERS = {"builtin", "jina", "firecrawl", "custom"}

# ---- 聊天图片输入（视觉模型，如 qwen3.6 系列）----
CHAT_IMAGE_MAX_FILES = int(os.getenv("CHAT_IMAGE_MAX_FILES", "4"))  # 单条消息最多图片数
CHAT_IMAGE_MAX_BYTES = int(os.getenv("CHAT_IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))  # 单图大小上限（压缩前）
CHAT_IMAGE_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
CHAT_VIDEO_MAX_BYTES = int(os.getenv("CHAT_VIDEO_MAX_BYTES", str(200 * 1024 * 1024)))
CHAT_VIDEO_MAX_FRAMES = int(os.getenv("CHAT_VIDEO_MAX_FRAMES", "8"))

# ---- 运行时覆盖（由前端 /api/config 动态设置，优先级高于 .env）----
# 模型选择由 /api/config 写入 STATE_FILE，服务启动时恢复；密钥不写入这里，
# 而是按项目存入 secrets_store 的受保护文件。code_root 等本地偏好也共用该状态文件。
_RUNTIME = {}

# ---- 请求级项目上下文（P3 端点项目化）----
# 让 get_runtime('code_root') 变成「请求上下文感知」：api.py 的 project_context_middleware
# 按 X-DocMind-Project（或 ?project_id=）解析出项目 root 后 set_context_code_root(root)，
# 于是 tools / regions / workbench_fs / agent 等几十处既有 get_runtime('code_root') 调用
# 零改动即自动项目化。
# 关键正确性前提（已用 tests/test_project_routing.py 固化）：FastAPI 的 run_in_threadpool
# （anyio.to_thread.run_sync）会复制当前 context，故异步端点内核里 await run_in_threadpool(…)
# 内部仍读到正确 root；而独立 daemon 线程（GPU/引擎看门狗）不继承请求上下文——这是**正确**
# 行为（它们本就非请求作用域）。
_CTX_CODE_ROOT: contextvars.ContextVar = contextvars.ContextVar("docmind_ctx_code_root", default=None)

#: 会话级代码库（比「按项目」更窄）。由 api 的会话入口按 session_id 绑定 ——
#: 读了该会话记录里记下的 code_root。空 = 未绑定，回落到项目/全局。
_CTX_SESSION_CODE_ROOT: contextvars.ContextVar = contextvars.ContextVar(
    "docmind_ctx_session_code_root", default=None)


def set_ctx_session_code_root(value):
    """把当前执行上下文绑定到某个**会话**的代码库；返回用于 reset 的 Token。

    ⚠ 与 `set_context_code_root`（按项目）**配对使用**，严格 try/finally。
    优先级：会话 > 项目 > 全局（见 `get_runtime`）。
    """
    return _CTX_SESSION_CODE_ROOT.set(value or None)


def reset_ctx_session_code_root(token) -> None:
    """按 Token 还原会话级上下文（必须与 set_ctx_session_code_root 配对）。"""
    try:
        _CTX_SESSION_CODE_ROOT.reset(token)
    except (ValueError, LookupError, RuntimeError):
        pass


def set_context_code_root(value):
    """把当前执行上下文绑定到某个项目 root；返回用于 reset 的 Token。"""
    return _CTX_CODE_ROOT.set(value)


def reset_context_code_root(token) -> None:
    """按 Token 还原上下文（必须与 set_context_code_root 配对，严格 try/finally）。"""
    try:
        _CTX_CODE_ROOT.reset(token)
    except (ValueError, LookupError, RuntimeError):
        pass


def get_runtime_excluding_session(key, default=None):
    """**不含会话级**的取值：只按 项目 > 全局。

    ⚠ 这是给「新会话首次绑定代码库」专用的。**不要**在别处用它替代
    `get_runtime` —— 那才是常规读取路径（含会话级）。

    为什么需要它：新会话绑定时要决定「用哪个库」，那一刻
    `get_runtime` 的第一优先级是**会话级**，而那个值可能是**上一个会话**
    泄漏下来的（同一进程内连续构造多个 Agent 时实测会发生）——
    于是新会话会继承上一个会话的代码库，比原缺陷更隐蔽。
    """
    if key == "code_root":
        ctx_root = _CTX_CODE_ROOT.get()
        if ctx_root:
            return ctx_root
    return _RUNTIME.get(key, default)


def set_runtime(key, value):
    _RUNTIME[key] = value


def get_runtime(key, default=None):
    # code_root 的优先级：**会话 > 请求上下文（按项目） > 全局**。
    #
    # ⚠⚠ **会话级优先是治「换了任务、检索器还在旧代码里翻」那个缺陷**（2026-10-10）。
    # 实测事故：一个会话的探针目录被设成全局 code_root，四天后同一项目的另一个新会话
    # 问别的事，中间件某一环没设成上下文 -> 静默用全局残留 -> 检索器在上一个任务的
    # 代码里搜，5 个子任务 0/5 全败。详见
    # `docs/known-defect-code-root-scope-too-coarse.md`。
    #
    # 为什么会话优先于项目：会话是**更窄**的作用域，而用户在一个会话里
    # 要的就是稳定用同一个库 —— 切项目不该让正在进行的对话换库。
    #
    # 读不到会话值时（老会话没这字段 / 还没落盘）依次回落项目、最后回落全局，
    # **与改动前行为一致**。这里不做任何静默改写。
    if key == "code_root":
        sess_root = _CTX_SESSION_CODE_ROOT.get()
        if sess_root:
            return sess_root
        ctx_root = _CTX_CODE_ROOT.get()
        if ctx_root:
            return ctx_root
    return _RUNTIME.get(key, default)


def reconcile_code_root_with_project(*, reason: str = "",
                                     allow_override: bool = True) -> dict:
    """顶层 `code_root` 与当前项目 root 不一致时，**以项目为准**改写顶层。

    ## 为什么需要（治本之后仍需要）

    `bbeed9b8` 把优先级改成 会话 > 项目 > 全局，请求路径上不会再串。
    但仍有两条路走不到项目上下文：

    * **启动迁移**：本模块导入期用顶层 `code_root` 建第一个项目；
    * **非请求场景**（测试 / 直调 / MCP 命令）：没有 `_CTX_CODE_ROOT`。

    这两条路上顶层值仍是"全局残留"。实测那次 mean 题就是把一个探针目录
    留在了顶层，四天后让另一个会话的检索器在错的目录里翻
    （见 `docs/known-defect-code-root-scope-too-coarse.md`）。

    ## 三条边界

    1. **「显式设置优先」是铁律，高于本条治标**。`allow_override=False` 时，
       顶层已被显式设过就一律不动 —— 调用方当次的意图不该被静默刷掉。
       （实测第一版没这道闸门，4 条 `test_explicit_code_root_*` 立刻红）
    2. 无当前项目 / 项目功能关闭时**什么都不做**：那是单项目模式，
       顶层就是唯一真源（`DOCMIND_PROJECTS=0` 一键回滚的语义）。
    3. 不一致时改写并回报；一致时空操作。返回 dict 便于调用方记账。
    """
    out = {"ok": True, "reason": reason or "", "changed": False,
           "before": "", "after": "", "project_root": ""}
    try:
        import projects                     # 局部 import 避开 config<->projects 循环依赖
        if not projects.enabled():
            out["note"] = "项目功能关闭（DOCMIND_PROJECTS=0），顶层是唯一真源"
            return out
        pid = projects.current_project_id()
        if not pid or pid == projects.LEGACY_ID:
            out["note"] = "无当前项目，不改顶层"
            return out
        rec = projects.get_project(pid) or {}
        proot = str(rec.get("root") or "").strip()
        if not proot:
            out["note"] = "当前项目没有 root，不改顶层"
            return out
        top = str(_RUNTIME.get("code_root") or "").strip()
        out["before"], out["project_root"] = top, proot
        if top and top != proot:
            if not allow_override:
                out["note"] = "顶层与项目不一致，但显式设置优先，不改"
            else:
                _RUNTIME["code_root"] = proot
                out["changed"] = True
        out["after"] = str(_RUNTIME.get("code_root") or "").strip()
    except Exception as exc:               # noqa: BLE001 - 对账失败不是致命错误
        out["ok"] = False
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


# ---- 跨重启持久化的本地状态 ----
# 与 .chroma 同目录（开发=源码根；冻结=_internal），只存路径类非敏感数据，
# 避免重启后必须重新选择代码库。写入失败一律静默回落内存态，不影响主流程。
STATE_FILE = os.path.join(STATE_ROOT, ".docmind_state.json")


def save_state(key, value):
    try:
        data = {}
        if os.path.isfile(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (ValueError, OSError):
                data = {}
        data[key] = value
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_state(key, default=None):
    """读取跨重启持久化状态中的单个键；缺失/损坏返回 default。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data.get(key, default)


# ---- 用户在模型设置里手填的上下文窗口覆盖：{"provider/model": tokens} ----
# 内置画像不可能覆盖所有模型（尤其自定义 OpenAI 端点），允许用户显式指定；
# 跨重启持久化在 STATE_FILE，优先级高于实时探测与内置画像。
_CONTEXT_WINDOW_OVERRIDES = {}
_MODEL_CAPABILITY_OVERRIDES = {}


def _ctx_override_key(provider: str, model: str) -> str:
    return f"{(provider or '').strip().lower()}/{(model or '').strip().lower()}"


def get_context_window_override(provider: str, model: str):
    """返回用户自定义窗口（int）；未设置返回 None。"""
    v = _CONTEXT_WINDOW_OVERRIDES.get(_ctx_override_key(provider, model))
    return int(v) if isinstance(v, int) and v > 0 else None


def set_context_window_override(provider: str, model: str, tokens):
    """设置/更新自定义窗口（tokens 为正整数）；0 或非法值等同清除。"""
    key = _ctx_override_key(provider, model)
    try:
        tokens = int(tokens)
    except (TypeError, ValueError):
        tokens = 0
    if tokens > 0:
        _CONTEXT_WINDOW_OVERRIDES[key] = tokens
    else:
        _CONTEXT_WINDOW_OVERRIDES.pop(key, None)
    save_state("context_window_overrides", dict(_CONTEXT_WINDOW_OVERRIDES))


def clear_context_window_override(provider: str, model: str):
    _CONTEXT_WINDOW_OVERRIDES.pop(_ctx_override_key(provider, model), None)
    save_state("context_window_overrides", dict(_CONTEXT_WINDOW_OVERRIDES))


def get_model_capability_override(provider: str, model: str):
    value = _MODEL_CAPABILITY_OVERRIDES.get(_ctx_override_key(provider, model))
    return dict(value) if isinstance(value, dict) else None


def set_model_capability_override(provider: str, model: str, *, thinking=None, vision=None, video=None):
    key = _ctx_override_key(provider, model)
    current = dict(_MODEL_CAPABILITY_OVERRIDES.get(key) or {})
    allowed = {
        "thinking": {"native", "toggle", "none", "unknown"},
        "vision": {"native", "harness", "none", "unknown"},
        "video": {"native", "frames", "none", "unknown"},
    }
    for name, value in (("thinking", thinking), ("vision", vision), ("video", video)):
        if value is None:
            continue
        if value not in allowed[name]:
            raise ValueError(f"无效的{name}能力值: {value}")
        current[name] = value
    if current:
        _MODEL_CAPABILITY_OVERRIDES[key] = current
    else:
        _MODEL_CAPABILITY_OVERRIDES.pop(key, None)
    save_state("model_capability_overrides", dict(_MODEL_CAPABILITY_OVERRIDES))


def clear_model_capability_override(provider: str, model: str):
    _MODEL_CAPABILITY_OVERRIDES.pop(_ctx_override_key(provider, model), None)
    save_state("model_capability_overrides", dict(_MODEL_CAPABILITY_OVERRIDES))


# ---- 已保存的模型预设（命名配置，供一键切换；密钥不在这里，走项目 secrets_store）----
# 结构：[{id, label, provider, model, base_url, context_window, created_at, updated_at}]
# context_window 为 0/缺省 = 自动（探测/画像）。预设全局存储（与 llm_provider 等选择一致），
# API Key 按项目存在 secrets_store 的 "preset:<id>" 槽位。
def _normalize_preset(raw, preset_id=None):
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider") or "").strip().lower()
    model = str(raw.get("model") or "").strip()
    if provider not in PROVIDERS:
        return None
    label = str(raw.get("label") or "").strip()[:40]
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if provider == "custom":
        if not base_url.startswith(("http://", "https://")) or not model:
            return None
    if not label:
        label = f"{PROVIDERS[provider].get('label', provider)} · {model or '默认模型'}"
    try:
        ctx = int(raw.get("context_window") or 0)
    except (TypeError, ValueError):
        ctx = 0
    if ctx and not (1024 <= ctx <= 2_097_152):
        ctx = 0
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "id": preset_id or str(raw.get("id") or "").strip()[:48],
        "label": label,
        "provider": provider,
        "model": model,
        "base_url": base_url if provider == "custom" else "",
        "context_window": ctx,
        "created_at": raw.get("created_at") or now,
        "updated_at": now,
    }


def load_model_presets():
    """读取全部模型预设；损坏/非法条目静默丢弃，永不抛错。"""
    raw = load_state("llm_model_presets", [])
    out = []
    if isinstance(raw, list):
        for item in raw:
            p = _normalize_preset(item)
            if p and p["id"]:
                out.append(p)
    return out


def save_model_presets(presets):
    save_state("llm_model_presets", presets)


def upsert_model_preset(preset):
    """新增/更新一个预设（id 为空时生成）；返回 (preset, error)。"""
    import uuid
    pid = str(preset.get("id") or "").strip()[:48] or "mp-" + uuid.uuid4().hex[:12]
    norm = _normalize_preset(preset, preset_id=pid)
    if norm is None:
        return None, "预设参数无效：需要已知厂商与模型名（自定义端点还要合法 http(s) 地址）。"
    items = load_model_presets()
    norm["created_at"] = next((x["created_at"] for x in items if x["id"] == pid), norm["created_at"])
    items = [x for x in items if x["id"] != pid]
    items.append(norm)
    items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    save_model_presets(items)
    return norm, ""


def remove_model_preset(preset_id):
    items = load_model_presets()
    kept = [x for x in items if x["id"] != preset_id]
    if len(kept) == len(items):
        return False
    save_model_presets(kept)
    return True


def _apply_persisted_state():
    """恢复上次持久化的本地选择（模型 / code_root / GPU 偏好 / 自定义窗口）。

    过去在 import config 时调用（导入期磁盘读）——现改由服务启动期显式调用
    （api.py 的 lifespan，紧接 ensure_dirs() 之后、gpu.init() 之前），
    使导入 config 不再触碰磁盘。函数本身与调用点解耦：路径失效自动忽略。
    """
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    root = data.get("code_root")
    # 顺序约束：调用方（如 verify_scene_canvas.py / verify_engine_embed.py / verify_regions.py
    # 的 serve()）若已在启动前 set_runtime('code_root', …) 显式指定代码库，则不得被持久化
    # 状态覆盖——显式设置优先于持久化恢复。这正是把本函数从 import 期移到 lifespan
    # （ensure_dirs() 之后）后必须补上的约束：脚本「先设 code_root、再启动应用」，其
    # set_runtime 早于 lifespan 执行，若无此判断会被这里的无条件赋值冲掉。
    # 其余键（GPU 偏好 / 自定义窗口）调用方不会预设，行为保持不变。
    if isinstance(root, str) and root and os.path.isdir(root) and "code_root" not in _RUNTIME:
        _RUNTIME["code_root"] = root
    # P2 迁移（**条件化**）：只有当项目注册表**不存在或为空**时（= 老用户首次升级的首次
    # 迁移），才用持久化 code_root 做 ensure_project + set_current。若已有注册表，则完全
    # 不迁移——保持用户持久化的 current_project_id（多项目下重启不得把 current 撞回 code_root
    # 对应的旧项目）；current 缺失/非法时由 projects.current_project_id() 的既有兜底取
    # last_opened 最新者，同样不撞回 code_root。用持久化的 root（磁盘真实状态）登记；显式
    # set_runtime('code_root', …) 的覆盖只影响运行时值，不影响此处登记。projects 局部 import
    # 以避开 config<->projects 的循环依赖，且不在导入期触发磁盘副作用。
    if isinstance(root, str) and root and os.path.isdir(root):
        try:
            import projects
            if projects.enabled() and not projects.list_projects():
                pid = projects.ensure_project(root)
                if pid:
                    projects.set_current(pid)
        except Exception:  # noqa: BLE001 —— 迁移失败绝不能阻断启动
            pass
        # 治标（2026-10-11，`known-defect-code-root-scope-too-coarse.md` §5）：
        # `reconcile_code_root_with_project()` 做「顶层与当前项目不一致时以项目为准」。
        #
        # ⚠⚠ **这里刻意不调用它**（实测后撤回调用点，函数本体保留）。
        #
        # 第一版在这里无条件跑，结果
        # `tests/test_api_model_config.py::ProjectKeyPersistenceTests::
        #  test_restart_loads_key_from_current_project_not_stale_code_root`
        # 立刻红。那条判据**故意**造出「向后兼容指针指向 app、注册表指向 game」
        # 的形状（原注释：This is the real failure shape），用来验证
        # 「key 从当前项目读，不被 code_root 带偏」—— 我这一刷恰好把它要测的
        # 前提抹掉了。
        #
        # 结论：启动路径上顶层 stale 值是**被依赖的测试与兼容形状**，
        # 不是需要治标的病。真正要治的是「无请求上下文的非请求场景」，
        # 那由调用方显式决定（见函数 docstring 的 allow_override）。
    # 模型选择：显式 set_runtime（例如启动脚本或测试传入）优先于持久化状态，
    # 持久化状态再优先于 .env 中的默认值。只接受已知 provider 和非空字符串，
    # 损坏的状态不会阻断服务启动。
    persisted_provider = data.get("llm_provider")
    if ("llm_provider" not in _RUNTIME and isinstance(persisted_provider, str)
            and persisted_provider in PROVIDERS):
        _RUNTIME["llm_provider"] = persisted_provider
    persisted_model = data.get("llm_model")
    if ("llm_model" not in _RUNTIME and isinstance(persisted_model, str)
            and persisted_model.strip()):
        _RUNTIME["llm_model"] = persisted_model.strip()
    persisted_base_url = data.get("llm_base_url")
    if ("llm_base_url" not in _RUNTIME and isinstance(persisted_base_url, str)
            and persisted_base_url.strip()):
        _RUNTIME["llm_base_url"] = persisted_base_url.strip().rstrip("/")
    persisted_embedding = data.get("embedding_provider")
    if ("embedding_provider" not in _RUNTIME and isinstance(persisted_embedding, str)
            and persisted_embedding in {"local", "ollama", "qwen"}):
        _RUNTIME["embedding_provider"] = persisted_embedding
    # GPU 空闲卸载/采样间隔为用户在 GPU 面板设置的本机偏好，跟随状态文件恢复
    for key in ("gpu_idle_unload_seconds", "gpu_poll_interval"):
        val = data.get(key)
        if isinstance(val, (int, float)) and val >= 0:
            _RUNTIME[key] = float(val)
    # 用户自定义的模型上下文窗口（键 "provider/model" -> 正整数）
    ov = data.get("context_window_overrides")
    if isinstance(ov, dict):
        for k, v in ov.items():
            if isinstance(k, str) and "/" in k and isinstance(v, int) and v > 0:
                _CONTEXT_WINDOW_OVERRIDES[k] = v
    cap_ov = data.get("model_capability_overrides")
    if isinstance(cap_ov, dict):
        for k, value in cap_ov.items():
            if isinstance(k, str) and "/" in k and isinstance(value, dict):
                clean = {n: v for n, v in value.items() if v in {
                    "native", "toggle", "none", "unknown", "harness", "frames"
                }}
                if clean:
                    _MODEL_CAPABILITY_OVERRIDES[k] = clean
    # 越界访问模式（safe/high）：UI 切换后跨重启恢复；显式 set_runtime 优先
    m = data.get("external_access_mode")
    if m in ("safe", "high") and "external_access_mode" not in _RUNTIME:
        _RUNTIME["external_access_mode"] = m
    # 模型选择模式：fixed=固定模型（默认）；auto=每轮按任务复杂度在本地/云端预设间自动路由
    mode = data.get("llm_mode")
    if mode in ("fixed", "auto") and "llm_mode" not in _RUNTIME:
        _RUNTIME["llm_mode"] = mode
    acp = data.get("auto_cloud_preset_id")
    if isinstance(acp, str) and acp.strip() and "auto_cloud_preset_id" not in _RUNTIME:
        _RUNTIME["auto_cloud_preset_id"] = acp.strip()
    # 自主开发舱专用模型预设：空=跟随全局模型；非空且模式为 fixed 时工作流会话单独挂该模型
    cpm = data.get("cockpit_preset_id")
    if isinstance(cpm, str) and "cockpit_preset_id" not in _RUNTIME:
        _RUNTIME["cockpit_preset_id"] = cpm.strip()
    # 开发舱模型模式：global=跟随全局；fixed=固定使用 cockpit_preset_id；
    # auto=Harness 按阶段/任务复杂度在全局模型与自动模式云端预设之间自选
    cmm = data.get("cockpit_mode")
    if isinstance(cmm, str) and cmm.strip() in ("global", "fixed", "auto") \
            and "cockpit_mode" not in _RUNTIME:
        _RUNTIME["cockpit_mode"] = cmm.strip()

    capacity = data.get("agent_capacity_settings")
    if isinstance(capacity, dict) and "agent_capacity_settings" not in _RUNTIME:
        from agent_runtime.capacity import normalize_settings, DEFAULTS
        # ⚠⚠ 持久化状态**可能含负值**（手改状态文件 / 旧版本写入 / 导入），
        # 而 normalize_settings 对负值是 raise（请求边界靠它 400 拒绝）；
        # 但这里是**开机恢复路径**，一个坏字段不能让服务起不来。
        # 所以 try 住并回落到 DEFAULTS，与 get_settings() 同一策略。
        try:
            _RUNTIME["agent_capacity_settings"] = normalize_settings(capacity)
        except ValueError:
            _RUNTIME["agent_capacity_settings"] = dict(DEFAULTS)

    # 网络搜索 / URL 获取配置恢复
    _apply_web_search_state(data)


# 注：过去此处 import 期直接调用 _apply_persisted_state()（导入即磁盘读）。
# 现改为由服务启动期显式调用（api.py lifespan，见 _app_lifespan），
# 使 `import config` 不产生任何磁盘 I/O；仅读取的持久化状态在进程真正
# 对外提供服务前恢复，行为对外等价。


def edit_confirm_enabled():
    """写工具是否处于「人工确认」模式：运行时覆盖优先，其次 .env EDIT_CONFIRM。"""
    v = get_runtime("edit_confirm")
    if v is not None:
        return bool(v)
    return EDIT_CONFIRM


def exec_approval_enabled():
    """python_exec 是否走「三档分级同意」护栏：运行时覆盖优先，其次 .env DOCMIND_EXEC_APPROVAL。
    关时退回旧的硬拦语义（越界/凭据一律拒），可一键回退。"""
    v = get_runtime("exec_approval")
    if v is not None:
        return bool(v)
    return EXEC_APPROVAL


def exec_tier_confirm(tier):
    """某档需要的连续确认次数：第一档 0（免批），第二档 EXEC_TIER2_CONFIRM，第三档 EXEC_TIER3_CONFIRM。"""
    t = int(tier)
    if t <= 1:
        return 0
    if t == 2:
        return EXEC_TIER2_CONFIRM
    return EXEC_TIER3_CONFIRM


# ---- AI 越界访问模式 ----
# safe：工具只能读写当前代码库（code_root）内文件，禁止任何项目外访问；
# high：在配置 DOCMIND_EXTERNAL_DIRS 白名单的前提下，允许对其他目录增删改查。
# 默认取 .env 的 DOCMIND_ACCESS_MODE；运行时经 /api/config 切换并持久化（见
# set_external_access_mode）。所有受控越界工具都须先过 get_external_access_mode()。
def get_external_access_mode():
    """返回 'safe' | 'high'：运行时覆盖优先，否则取 .env DOCMIND_ACCESS_MODE。"""
    v = get_runtime("external_access_mode")
    if v in ("safe", "high"):
        return v
    return EXTERNAL_ACCESS_MODE


def external_access_high():
    """便捷判断：当前是否处于高权限（越界）模式。"""
    return get_external_access_mode() == "high"


def set_external_access_mode(mode):
    """切换越界访问模式：'safe' 或 'high'；非法值回落 'safe'。跨重启持久化。"""
    mode = (mode or "").strip().lower()
    if mode not in ("safe", "high"):
        mode = "safe"
    set_runtime("external_access_mode", mode)
    save_state("external_access_mode", mode)
    return mode


# ---- 网络搜索 / URL 获取配置运行时覆盖 ----
def _persist_web_search():
    """把当前网络搜索运行时覆盖写回 STATE_FILE（供多个 setter 复用）。"""
    save_state("web_search_provider", get_runtime("web_search_provider") or WEB_SEARCH_PROVIDER)
    save_state("web_search_api_key", get_runtime("web_search_api_key") or WEB_SEARCH_API_KEY)
    save_state("web_search_api_url", get_runtime("web_search_api_url") or WEB_SEARCH_API_URL)
    save_state("web_search_prefer_builtin", bool(get_runtime("web_search_prefer_builtin")))


def _persist_web_fetch():
    save_state("web_fetch_provider", get_runtime("web_fetch_provider") or WEB_FETCH_PROVIDER)
    save_state("web_fetch_api_key", get_runtime("web_fetch_api_key") or WEB_FETCH_API_KEY)
    save_state("web_fetch_api_url", get_runtime("web_fetch_api_url") or WEB_FETCH_API_URL)


def get_web_search_provider():
    v = get_runtime("web_search_provider")
    if v in _WEB_SEARCH_PROVIDERS:
        return v
    return WEB_SEARCH_PROVIDER if WEB_SEARCH_PROVIDER in _WEB_SEARCH_PROVIDERS else "builtin_auto"


def set_web_search_provider(provider):
    provider = (provider or "").strip().lower()
    if provider not in _WEB_SEARCH_PROVIDERS:
        provider = "builtin_auto"
    set_runtime("web_search_provider", provider)
    save_state("web_search_provider", provider)
    return provider


def get_web_search_api_key():
    return get_runtime("web_search_api_key") or WEB_SEARCH_API_KEY or ""


def set_web_search_api_key(key):
    key = (key or "").strip()
    set_runtime("web_search_api_key", key)
    save_state("web_search_api_key", key)
    return key


def get_web_search_api_url():
    return get_runtime("web_search_api_url") or WEB_SEARCH_API_URL or ""


def set_web_search_api_url(url):
    url = (url or "").strip().rstrip("/")
    set_runtime("web_search_api_url", url)
    save_state("web_search_api_url", url)
    return url


def get_web_search_prefer_builtin():
    v = get_runtime("web_search_prefer_builtin")
    if v is not None:
        return bool(v)
    return WEB_SEARCH_PREFER_BUILTIN


def set_web_search_prefer_builtin(enabled):
    enabled = bool(enabled)
    set_runtime("web_search_prefer_builtin", enabled)
    save_state("web_search_prefer_builtin", enabled)
    return enabled


def get_web_fetch_provider():
    v = get_runtime("web_fetch_provider")
    if v in _WEB_FETCH_PROVIDERS:
        return v
    return WEB_FETCH_PROVIDER if WEB_FETCH_PROVIDER in _WEB_FETCH_PROVIDERS else "builtin"


def set_web_fetch_provider(provider):
    provider = (provider or "").strip().lower()
    if provider not in _WEB_FETCH_PROVIDERS:
        provider = "builtin"
    set_runtime("web_fetch_provider", provider)
    save_state("web_fetch_provider", provider)
    return provider


def get_web_fetch_api_key():
    return get_runtime("web_fetch_api_key") or WEB_FETCH_API_KEY or ""


def set_web_fetch_api_key(key):
    key = (key or "").strip()
    set_runtime("web_fetch_api_key", key)
    save_state("web_fetch_api_key", key)
    return key


def get_web_fetch_api_url():
    return get_runtime("web_fetch_api_url") or WEB_FETCH_API_URL or ""


def set_web_fetch_api_url(url):
    url = (url or "").strip().rstrip("/")
    set_runtime("web_fetch_api_url", url)
    save_state("web_fetch_api_url", url)
    return url


def _apply_web_search_state(data):
    """从 STATE_FILE 恢复网络搜索配置；显式 set_runtime 优先（同 external_access_mode）。"""
    for key in ("web_search_provider", "web_search_api_key", "web_search_api_url",
                "web_fetch_provider", "web_fetch_api_key", "web_fetch_api_url"):
        v = data.get(key)
        if v is not None and key not in _RUNTIME:
            _RUNTIME[key] = v
    pb = data.get("web_search_prefer_builtin")
    if pb is not None and "web_search_prefer_builtin" not in _RUNTIME:
        _RUNTIME["web_search_prefer_builtin"] = bool(pb)
