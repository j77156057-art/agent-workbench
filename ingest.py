"""文档摄取：加载(PDF/Markdown/TXT) -> 切分 -> 向量化 -> 入库。

另含「代码模式」摄取：把源代码/配置文件按文件 + 函数/类切成带符号名的片段，
存入独立的代码集合，使 Agent 能检索/阅读/搜索你的工程代码。
"""
import ast
import os
import re
import uuid
import json

from config import CHUNK_SIZE, CHUNK_OVERLAP, CODE_COLLECTION_NAME, CODE_CHUNK
from embeddings import EmbeddingClient
from vectorstore import add_documents
from data_formats import FORMAT_EXTENSIONS, DataFormatError, read_file


def load_text(path):
    """读取文档/表格/结构化文件并转成稳定的可检索文本。"""
    parsed = read_file(path)
    kind = parsed.get("kind")
    if kind == "text":
        return str(parsed.get("text") or "")
    if kind == "table":
        lines = ["\t".join(str(value if value is not None else "") for value in parsed.get("headers", []))]
        lines.extend("\t".join(str(value if value is not None else "") for value in row)
                     for row in parsed.get("rows", []))
        return "\n".join(lines)
    if kind == "workbook":
        blocks = []
        for name, table in (parsed.get("sheets") or {}).items():
            blocks.append(f"# Sheet: {name}")
            blocks.append(load_text_from_table(table))
        return "\n\n".join(blocks)
    if kind == "slides":
        return "\n\n".join(f"# Slide {index}\n{text}" for index, text in enumerate(parsed.get("slides") or [], 1))
    if kind in {"json", "yaml", "toml", "xml", "records"}:
        if parsed.get("table") or parsed.get("headers"):
            return load_text_from_table(parsed)
        # 解析失败的脏结构化文件仍保留原文和错误位置，避免把坏 JSON
        # 退化成单独的 ``null`` 后丢失可检索线索。
        if parsed.get("errors") and parsed.get("text"):
            errors = "\n".join(
                f"- {item.get('error', '解析失败')}"
                + (f"（第 {item['line']} 行，第 {item['column']} 列）"
                   if item.get("line") else "")
                for item in parsed.get("errors", [])[:20]
                if isinstance(item, dict)
            )
            return f"{parsed.get('text', '')}\n\n[解析错误]\n{errors}".strip()
        value = parsed.get("value", parsed.get("records", parsed.get("root", "")))
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if parsed.get("preview"):
        return str(parsed["preview"])
    raise DataFormatError(f"文件没有可提取的文本：{path}")


def load_text_from_table(table):
    lines = ["\t".join(str(value if value is not None else "") for value in table.get("headers", []))]
    lines.extend("\t".join(str(value if value is not None else "") for value in row)
                 for row in table.get("rows", []))
    return "\n".join(lines)


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """优先按段落切分，超长段落再按字符长度兜底切分。"""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) <= size:
            cur += p + "\n"
        else:
            if cur.strip():
                chunks.append(cur.strip())
            if len(p) > size:
                step = max(1, size - overlap)
                for i in range(0, len(p), step):
                    piece = p[i : i + size].strip()
                    if piece:
                        chunks.append(piece)
                cur = ""
            else:
                cur = p + "\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _reject_degraded_embeddings(emb):
    """拒绝用**意外降级**的向量入库。

    为什么这条必须存在：`add_documents` 里的维度冲突检查只保护"集合已存在"的
    情况。集合为空或首次建库时，Chroma 会把维度**钉死在首次 insert 的值**上——
    于是 Ollama 掉线期间上传的文档，会以 256 维哈希词袋（**无语义**）写进库里，
    **全程不报任何错**。用户看到的是"上传成功"，之后知识库检索静默返回垃圾。

    只拦"降级"，不拦"用户主动选 local"：后者是一个决定，且维度自洽。
    """
    if not getattr(emb, "degraded", False):
        return
    raise ValueError(
        "向量模型当前不可用，已停止入库（否则会写入无意义的哈希向量并把集合"
        "维度钉死，之后检索会静默返回错误结果）。\n"
        "原因：%s\n"
        "处理办法：\n"
        "  1) 启动 Ollama 并确认已拉取嵌入模型，然后重新上传；\n"
        "  2) 或在「⚙ 模型设置 → 知识库检索后端」里改用云端嵌入；\n"
        "  3) 或明确选择「不启用（本地哈希兜底）」——那样所有文档都在同一维度下，"
        "检索退化为字面词匹配，但至少结果是真的。"
        % (getattr(emb, "degraded_reason", "") or "嵌入后端不可用")
    )


def ingest_file(path, emb=None, size=None, overlap=None):
    """摄取单个文件，返回切片数量。"""
    emb = emb or EmbeddingClient()
    _reject_degraded_embeddings(emb)
    size = size or CHUNK_SIZE
    overlap = overlap or CHUNK_OVERLAP

    text = load_text(path)
    chunks = chunk_text(text, size, overlap)
    embeddings = emb.embed(chunks)
    metas = [{"source": os.path.basename(path), "chunk": i} for i in range(len(chunks))]
    ids = [uuid.uuid4().hex for _ in chunks]
    add_documents(chunks, embeddings, metas, ids)
    return len(chunks)


def ingest_directory(directory):
    """批量摄取目录下的所有支持文件。"""
    total = 0
    for name in sorted(os.listdir(directory)):
        p = os.path.join(directory, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in {
                ext for ext, fmt in FORMAT_EXTENSIONS.items()
                if fmt in {"pdf", "text", "markdown", "csv", "tsv", "json", "jsonl", "yaml", "toml", "xml", "html", "docx", "xlsx", "pptx"}}:
            try:
                total += ingest_file(p)
            except Exception as e:  # 单个文件失败不影响其他
                print(f"[ingest] 跳过 {name}: {e}")
    return total


# ---------------------------------------------------------------------------
# 代码模式摄取：把源代码/配置文件切成带「符号名」的片段，存入独立代码集合。
# 设计要点：
#   - 散文分块按段落即可，但代码必须按「函数/类」切，否则一个函数被腰斩、
#     两个不相关的函数被粘成一段，检索质量会崩。
#   - Python 用 ast 精确拿到每个 def/class（含方法）的起止行与名字；
#     其它语言用正则启发式识别定义行，按定义切分。
#   - 切片过大时再按行兜底切，保证单条不超 CODE_CHUNK。
# ---------------------------------------------------------------------------

# 纳入代码库索引的扩展名（含 .json/.yaml/.toml 等配置文件，它们是「数据驱动」的核心）
_CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".java", ".cpp", ".cc", ".c",
    ".h", ".hpp", ".go", ".rs", ".lua", ".rb", ".php", ".swift", ".kt", ".scala",
    ".sh", ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml",
    ".csv", ".tsv", ".xml", ".html", ".htm", ".md", ".markdown",
    # Godot：GDScript 脚本、着色器、文本场景/资源与工程配置（二进制 .scn/.res 天然排除）
    ".gd", ".gdshader", ".tscn", ".tres", ".godot",
}
# 遍历时跳过的目录（依赖/构建产物/版本控制/虚拟环境/向量库自身/归档备份）
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist",
    "bin", "obj", "Library", "Temp", ".idea", ".vscode", "target", "out",
    ".mypy_cache", ".ruff_cache",
    ".chroma", "_archived_builds", "uploads",  # 索引自身/归档备份/上传临时目录，避免污染代码集合
}
# 单个代码文件超过此体积（字节）则跳过，避免把巨型生成文件/压缩包塞进索引
_MAX_CODE_FILE = 500_000

# 扩展名 -> 语言标记（用于每块 metadata 的 lang 字段，便于检索排序与展示）
_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".cs": "csharp", ".java": "java", ".cpp": "cpp",
    ".cc": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp", ".go": "go", ".rs": "rust",
    ".lua": "lua", ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".sh": "shell", ".json": "json", ".jsonl": "jsonl",
    ".ndjson": "jsonl", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".csv": "csv", ".tsv": "tsv", ".xml": "xml", ".html": "html",
    ".htm": "html", ".md": "markdown", ".markdown": "markdown",
    ".gd": "gdscript", ".gdshader": "gdshader", ".tscn": "godot-scene",
    ".tres": "godot-resource", ".godot": "ini",
}

# 顶层/类型定义行的启发式前缀（用于非 Python 语言的切分）
_DEF_RE = re.compile(
    r"""^\s*(?:export\s+|public\s+|private\s+|protected\s+|internal\s+|static\s+"""
    r"""|async\s+|final\s+|virtual\s+|override\s+|inline\s+|const\s+|def\s+"""
    r"""|func(?:tion)?\s+|fn\s+|function\s+|class\s+|struct\s+|interface\s+"""
    r"""|impl\s+|module\s+|type\s+)""",
    re.X,
)


def _symbol_name(line, ext):
    """从一行定义里尽量抽出符号名（函数/类/方法/常量）。"""
    m = re.search(r"(?:def|func(?:tion)?|fn|function)\s+([A-Za-z_]\w*)", line)
    if m:
        return m.group(1)
    m = re.search(r"(?:class|struct|interface|impl|module|type)\s+([A-Za-z_]\w*)", line)
    if m:
        return m.group(1)
    m = re.search(
        r"(?:public|private|protected|internal|static|virtual|override|final|async|inline)\s+"
        r"[\w<>\[\],\s\.\?]+\s+([A-Za-z_]\w*)\s*\(",
        line,
    )
    if m:
        return m.group(1)
    m = re.search(r"const\s+([A-Za-z_]\w*)\s*=", line)
    if m:
        return m.group(1)
    # Java/Kotlin 构造器：访问修饰符后直接跟大写类名 + (（无返回类型，
    # 与「修饰符 + 返回类型 + 方法名 (」的普通方法互斥）
    m = re.match(r"^\s*(?:public|protected|private)\s+([A-Z][A-Za-z0-9_$]*)\s*\(", line)
    if m:
        return m.group(1)
    # Java static final 常量（含泛型/数组类型，如 private static final Map<K,V> KEY = …）
    m = re.search(
        r"\bstatic\s+final\b[\w<>\[\],.\s?]*?\s([A-Za-z_][\w$]*)\s*=",
        line,
    )
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z_]\w*)\s*:\s*(?:\(|async\s*\()", line)  # TS 方法简写
    if m:
        return m.group(1)
    return ""


def _extract_imports(text, ext):
    """抽取该文件 import 的顶层模块名列表（跨文件依赖追踪用，最多取前 20 个）。

    用于给每个切片附加 imports 元数据，回答"这个符号依赖哪些模块 / 从哪来"。
    Python 走 ast 精确提取；其它语言用通用正则兜底（import/from/using/require/include）。
    """
    mods = []
    if ext == ".py":
        try:
            tree = ast.parse(text)
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        mods.append(a.name.split(".")[0])
                elif isinstance(n, ast.ImportFrom):
                    if n.module:
                        mods.append(n.module.split(".")[0])
        except SyntaxError:
            pass
    else:
        # 通用正则：捕获 import/using/require 后的模块名（跳过关键字本身）
        pat = re.compile(r"(?:import|from|using|require)\s*['\"]?([\w./\-]+)", re.I)
        for m in pat.finditer(text):
            nm = m.group(1).split(".")[0].split("/")[-1].split("\\")[-1]
            if nm and nm.lower() not in ("from",):
                mods.append(nm)
    seen, out = set(), []
    for m in mods:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out[:20]


def _split_by_chars(text, size):
    """极长行（无空行无换行可切）按字符硬切，保证单块不超 size。"""
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


def _split_big(text, size):
    """把过长文本切成若干 ≤ size 的片段（递归再切，保持片段尽量内聚）。

    策略：优先在空行（自然边界）处断开；若一段仍超 size 则对这段递归；
    没有任何空行可借力时退化为按行切；单行超长再按字符硬切。
    注意：符号名由调用方在切分后逐片附加，因此无论怎么切都不会丢符号。
    """
    if len(text) <= size:
        return [text]
    # 1) 优先按空行（自然段落边界）切
    parts = [p for p in re.split(r"\n[ \t]*\n", text) if p.strip()]
    if len(parts) > 1:
        chunks, buf = [], ""
        for p in parts:
            if len(buf) + len(p) + 2 <= size:
                buf = (buf + "\n\n" + p) if buf else p
            else:
                if buf:
                    chunks.extend(_split_big(buf, size))
                buf = p
        if buf:
            chunks.extend(_split_big(buf, size))
        return [c for c in chunks if c.strip()] or [text]
    # 2) 无空行：按行切；若某行单独超长则按字符硬切
    lines = text.split("\n")
    chunks, buf = [], ""
    for ln in lines:
        if len(buf) + len(ln) + 1 <= size:
            buf = (buf + "\n" + ln) if buf else ln
        else:
            if buf:
                chunks.append(buf)
            if len(ln) > size:
                chunks.extend(_split_by_chars(ln, size))
                buf = ""
            else:
                buf = ln
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()] or [text]


def _pieces_with_lines(raw, base_line, sym, pieces):
    """把 _split_big 产出的片段按其在 raw 中的位置换算成 1 基起止行号。

    返回 (片段, 符号名, 起始行, 结束行) 四元组；片段顺序出现，用游标定位，
    重复文本也不会错位。
    """
    out = []
    cursor = 0
    for piece in pieces:
        idx = raw.find(piece, cursor)
        if idx < 0:
            idx = cursor
        offset = raw.count("\n", 0, idx)
        start = base_line + offset
        end = start + piece.count("\n")
        out.append((piece, sym, start, end))
        cursor = idx + len(piece)
    return out


def _chunk_python(text):
    """用 ast 精确切分 Python 文件为（片段, 符号名, 起始行, 结束行）列表；失败返回 None。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.split("\n")
    nodes = []
    for node in tree.body:
        nodes.append(node)
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes.append(child)
    nodes.sort(key=lambda n: n.lineno)
    chunks = []
    # 文件头（imports / 模块 docstring / 注释）作为一段 preamble
    if nodes:
        pre_raw = "\n".join(lines[: nodes[0].lineno - 1])
        if pre_raw.strip():
            chunks.extend(
                _pieces_with_lines(pre_raw, 1, "", _split_big(pre_raw.strip(), CODE_CHUNK))
            )
    for n in nodes:
        start = n.lineno - 1
        end = getattr(n, "end_lineno", n.lineno)
        raw = "\n".join(lines[start:end])
        sym = n.name if hasattr(n, "name") else ""
        chunks.extend(
            _pieces_with_lines(raw, n.lineno, sym, _split_big(raw.strip(), CODE_CHUNK))
        )
    return chunks or None


def chunk_code(text, path):
    """把源代码切成（片段, 符号名, 起始行, 结束行）列表；优先 ast（Python），否则正则启发式。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        py = _chunk_python(text)
        if py is not None:
            return py
    lines = text.split("\n")
    idxs = [i for i, ln in enumerate(lines) if _DEF_RE.match(ln)]
    chunks = []
    if idxs and idxs[0] > 0:
        pre_raw = "\n".join(lines[: idxs[0]])
        if pre_raw.strip():
            chunks.extend(
                _pieces_with_lines(pre_raw, 1, "", _split_big(pre_raw.strip(), CODE_CHUNK))
            )
    for k, s in enumerate(idxs):
        e = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        raw = "\n".join(lines[s:e])
        sym = _symbol_name(lines[s], ext)
        chunks.extend(
            _pieces_with_lines(raw, s + 1, sym, _split_big(raw.strip(), CODE_CHUNK))
        )
    if not chunks:
        chunks.extend(
            _pieces_with_lines(text, 1, "", _split_big(text.strip() or text, CODE_CHUNK))
        )
    return chunks


def load_code_file(path):
    parsed = read_file(path)
    text = str(parsed.get("text") or "")
    if text.startswith("\ufeff"):
        text = text[1:]  # Godot 默认 UTF-8 BOM，剥掉避免污染首块
    return text


# ---------------------------------------------------------------------------
# 符号级切片（P1）：.gd/.py 走 symbols.extract 的精确符号表——
# 每个函数/类一个向量片段（过大再切），连续的 var/const/signal/enum 合并为
# 「声明批」片段；类头/文件头与未覆盖代码补 file/code 片段。检索可精确命中符号。
# 任何异常由调用方回退到 chunk_code 启发式切片，绝不因解析器问题丢索引。
# ---------------------------------------------------------------------------

_BLOCK_KINDS = {"class", "function"}
# 可合并成一个「声明批」片段的叶子符号
_LEAF_KINDS = {"const", "var", "signal", "enum", "group"}
_LEAF_BATCH_MAX_LINES = 60
_LEAF_BATCH_MAX_GAP = 3   # 两符号间隔超过 2 个空行则分批
_LEAF_BATCH_MAX_NAMES = 6


def _symbol_segments(symbols, line_count):
    """把符号表转成互斥/可嵌套的 (lo, hi, symbol, kind, signature, doc) 段。

    lo/hi 为 0 基半开行区间。类段可嵌套方法段（有意重复索引，沿用 Python 旧行为）。
    """
    segs = []
    leaf_run = []  # 当前连续叶子符号

    def flush_leaf(run):
        if not run:
            return
        lo = run[0]["start"] - 1
        hi = max(s["end"] for s in run)
        names = [s["name"] for s in run if s["kind"] != "group"]
        if len(names) > _LEAF_BATCH_MAX_NAMES:
            sym = ", ".join(names[:_LEAF_BATCH_MAX_NAMES]) + f" …(+{len(names)-_LEAF_BATCH_MAX_NAMES})"
        else:
            sym = ", ".join(names)
        segs.append((lo, hi, sym, "decl", "", ""))

    for s in sorted(symbols, key=lambda x: (x["start"], x["end"])):
        lo, hi = s["start"] - 1, s["end"]
        if s["kind"] in _BLOCK_KINDS:
            flush_leaf(leaf_run)
            leaf_run = []
            segs.append((lo, hi, s["name"], s["kind"], s.get("signature", ""), s.get("doc", "")))
        elif s["kind"] in _LEAF_KINDS:
            if leaf_run:
                prev = leaf_run[-1]
                too_far = lo - (prev["end"] - 1) > _LEAF_BATCH_MAX_GAP
                too_big = (max(s["end"] for s in leaf_run + [s]) - leaf_run[0]["start"] + 1
                           > _LEAF_BATCH_MAX_LINES)
                diff_parent = s.get("parent", "") != leaf_run[0].get("parent", "")
                if too_far or too_big or diff_parent:
                    flush_leaf(leaf_run)
                    leaf_run = []
            leaf_run.append(s)
        # 其它种类（node/section 等）不进结构化切片
    flush_leaf(leaf_run)
    segs.sort(key=lambda x: x[0])
    return segs


def _emit_span(lines, lo, hi, sym, kind, signature, doc):
    """把一段源码按 CODE_CHUNK 切成 (text, sym, sl, el, kind, sig, doc) 七元组。"""
    raw = "\n".join(lines[lo:hi])
    if not raw.strip():
        return []
    base_line = lo + 1
    out = []
    for piece, p_sym, sl, el in _pieces_with_lines(raw, base_line, sym,
                                                   _split_big(raw.strip(), CODE_CHUNK)):
        out.append((piece, p_sym, sl, el, kind, signature, doc))
    return out


def _symbol_code_chunks(text, path):
    """结构化符号切片；不支持的类型/无符号返回 None（调用方回退启发式）。"""
    import symbols as _symbols

    ext = os.path.splitext(path)[1].lower()
    if ext not in _symbols.STRUCTURED_CODE_EXT:
        return None
    env = _symbols.extract(text, path)
    syms = env.get("symbols") or []
    if not syms:
        return None
    lines = text.split("\n")
    segs = _symbol_segments(syms, len(lines))
    if not segs:
        return None

    out = []
    pos = 0
    header_sym = env.get("class_name") or ""
    for lo, hi, sym, kind, sig, doc in segs:
        if lo < pos:
            # 嵌套段（如类内方法）：与外层段有意重叠，不重复补间隙
            pass
        elif lo > pos:
            gap_raw = "\n".join(lines[pos:lo])
            if gap_raw.strip():
                out.extend(_emit_span(lines, pos, lo, header_sym if pos == 0 else "",
                                      "file" if pos == 0 else "code", "", ""))
        out.extend(_emit_span(lines, lo, hi, sym, kind, sig, doc))
        pos = max(pos, hi)
    if pos < len(lines):
        tail_raw = "\n".join(lines[pos:])
        if tail_raw.strip():
            out.extend(_emit_span(lines, pos, len(lines), "", "code", "", ""))
    return out or None


def _code_file_records(path, root, emb):
    """构建单个代码文件的 (chunks, embeddings, metas, ids)；无内容返回 None。"""
    text = load_code_file(path)
    ext = os.path.splitext(path)[1].lower()
    lang = _LANG_BY_EXT.get(ext, "text")
    structured = None
    try:
        structured = _symbol_code_chunks(text, path)
    except Exception as e:
        print(f"[ingest_code] 符号切片失败，回退启发式 {path}: {e}")
    chunks = structured if structured is not None else chunk_code(text, path)
    if not chunks:
        return None
    rel = os.path.relpath(path, root).replace("\\", "/")
    imports_str = ", ".join(_extract_imports(text, ext)) or "none"

    rows = []
    if structured is not None:
        for piece, sym, sl, el, kind, sig, doc in chunks:
            rows.append((piece, sym, sl, el, kind, sig or "", doc or ""))
    else:
        for piece, sym, sl, el in chunks:
            rows.append((piece, sym, sl, el, "code", "", ""))

    texts = [r[0] for r in rows]
    # 嵌入输入：把符号文档注释前置进向量文本（中文语义检索的关键，如「暴击怎么算」），
    # 但入库文档仍用原文，行号锚点不受影响
    emb_inputs = [
        ((r[6] + "\n" + r[0]) if r[6] else r[0])
        for r in rows
    ]
    embeddings = emb.embed(emb_inputs)
    # 每块带语言/import 边/起止行/符号种类/签名/文档，检索可精确到符号并解释其语义
    metas = [
        {
            "source": rel,
            "symbol": sym,
            "kind": kind,
            "lang": lang,
            "imports": imports_str,
            "start_line": sl,
            "end_line": el,
            "signature": signature,
            "doc": doc,
        }
        for _, sym, sl, el, kind, signature, doc in rows
    ]
    ids = [uuid.uuid4().hex for _ in rows]
    return texts, embeddings, metas, ids


def ingest_code_file(path, root, emb=None, collection=CODE_COLLECTION_NAME):
    """摄取单个代码文件，返回切片数量。"""
    emb = emb or EmbeddingClient()
    _reject_degraded_embeddings(emb)
    rec = _code_file_records(path, root, emb)
    if not rec:
        return 0
    texts, embeddings, metas, ids = rec
    add_documents(texts, embeddings, metas, ids, collection=collection)
    return len(texts)


# 目录级索引的「单次写入」批次上限：把切片累积到一定量后一次性 add。
# chroma 1.x 每次 add 都会重写索引，若按「每文件一次 add」做大量增量写入会极慢甚至卡死；
# 累积成较大的批次再 add 可避免该问题，同时限制大工程的内存占用。
_CODE_ADD_BATCH = 2000


def ingest_code_directory(root, emb=None, collection=CODE_COLLECTION_NAME):
    """遍历代码根目录，索引所有受支持的文件；返回总切片数。

    所有切片先在内存中累积，按 _CODE_ADD_BATCH 批量写入向量库（而非每文件一次 add）。
    """
    emb = emb or EmbeddingClient()
    _reject_degraded_embeddings(emb)
    root = os.path.abspath(root)
    total = 0
    texts_buf, embs_buf, metas_buf, ids_buf = [], [], [], []

    def _flush():
        nonlocal texts_buf, embs_buf, metas_buf, ids_buf
        if texts_buf:
            add_documents(texts_buf, embs_buf, metas_buf, ids_buf, collection=collection)
        texts_buf, embs_buf, metas_buf, ids_buf = [], [], [], []

    # `.docmind/` 是 Agent 工作台 自己的状态根：里面既有临时/状态目录（project_stages /
    # projects 等工作流副本、检查点、headcheck 影子整仓 —— 实测单个 project_stages
    # 就 752MB/6978 文件），也有**合法产物**（Godot Web 导出 .docmind/web/）。建库时
    # 若照单全收，会把项目原文件与 .docmind 里的副本双双 embed 进同一集合，让
    # search_code 返回重复 / 陈旧命中 —— 这正是 grep 之前踩过的同一坑。此处与
    # grep/glob 同源：按名修剪**状态目录**，产物目录照常入库；复用 code_intel 的
    # 状态目录名单（黑名单，非白名单 —— 白名单会让新出现的产物目录重新变成静默假否定）。
    # 注意：状态目录名单只覆盖 `.docmind/` 下这一层；**通用点目录（.workbuddy 等）与点
    # 文件（.docmind_*.jsonl）由下面遍历里的"剪点目录 + 跳点文件"另行处理**，与 grep 的
    # _walk_source_files 剪点目录口径对齐（见下）。
    try:
        from agent_runtime import code_intel as _ci
        _state_dirs = _ci._DOCMIND_STATE_DIRS
        _state_prefixes = _ci._DOCMIND_STATE_PREFIXES
    except Exception:  # noqa: BLE001 —— 拿不到就当无额外状态目录，绝不影响建库主流程
        _state_dirs = frozenset()
        _state_prefixes = ()

    for dp, dns, fns in os.walk(root):
        # 修剪与 grep 建索引层（code_intel._walk_source_files:725）对齐：剪 _SKIP_DIRS，
        # 再剪**所有点开头目录**——但单独放行 `.docmind`（它的产物子目录 web/ 需照常入库，
        # 只在其直接下一层按名剪状态目录）。原实现只剪 _SKIP_DIRS、不剪点目录，于是 Agent
        # 私有记忆 `.workbuddy/memory/*.md` 被整篇 embed 进语义向量库、被 search_code 当
        # "项目代码片段"命中引用（RETR-Pollution）；grep 那条一直剪点目录，这里补齐口径。
        dns[:] = [d for d in dns
                  if d not in _SKIP_DIRS and (d == ".docmind" or not d.startswith("."))]
        # 仅在 `.docmind/` 直接下一层修剪状态目录（产物目录照常入库，黑名单非白名单）。
        if os.path.relpath(dp, root).replace("\\", "/") == ".docmind":
            pruned = [d for d in dns
                      if d in _state_dirs or d.startswith(_state_prefixes)]
            if pruned:
                dns[:] = [d for d in dns if d not in pruned]
                print("[ingest_code] 已跳过 .docmind 状态目录（产物目录照常入库）："
                      "%s" % "、.docmind/".join(sorted(set(pruned))))
        for fn in sorted(fns):
            # 跳过以点开头的文件：`.docmind_mcp.json` / `.docmind_runtime.jsonl` /
            # `.docmind_tasks.jsonl` 这类 Agent 工作台 运行时/遥测台账不是项目源码，
            # 不该进语义库被 search_code 当权威来源返回（RETR-Pollution 点文件侧）。
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() not in _CODE_EXT:
                continue
            fp = os.path.join(dp, fn)
            try:
                if os.path.getsize(fp) > _MAX_CODE_FILE:
                    continue
                rec = _code_file_records(fp, root, emb)
                if rec:
                    texts, embeddings, metas, ids = rec
                    texts_buf.extend(texts)
                    embs_buf.extend(embeddings)
                    metas_buf.extend(metas)
                    ids_buf.extend(ids)
                    total += len(texts)
                    if len(texts_buf) >= _CODE_ADD_BATCH:
                        _flush()
            except Exception as e:  # 单个文件失败不影响其他
                print(f"[ingest_code] 跳过 {fn}: {e}")
    _flush()
    return total


def load_project_rules(root):
    """读取代码根目录下的 DOCMIND_RULES.md 作为「项目级规则」；不存在返回空串。

    规则由 /api/ingest_code 索引时读入并注入 Agent 的系统消息，使不同项目的
    分区约定 / 修改约束能随项目自动生效，无需改代码。
    """
    path = os.path.join(root, "DOCMIND_RULES.md")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return ""
