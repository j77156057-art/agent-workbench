"""代码符号提取：文件大纲、全局符号语义地图、符号级切片共用一份行级符号表。

零第三方依赖：
  - Python 走标准库 ast，行号/签名/文档串精确；
  - GDScript 是缩进语法、定义形式规整，用行级状态解析即可可靠提取
    （func/class_name/extends/inner class/signal/enum/const/var/@export/@export_group）；
  - Godot 文本场景/资源（.tscn/.tres）抽 node/resource 段；
  - 其它语言用正则 + 括号配平兜底。

符号统一为 dict：
  name, kind, start, end（均 1 基含端点）, parent, signature, doc, detail
文件信封：lang, class_name, extends, doc, symbols（按行号排序）。

文件结果按 (mtime_ns, size) 缓存，长驻进程里反复打开/画地图不重复解析。
"""
import ast
import os
import re

# ---------------------------------------------------------------- 常量

# 结构化符号种类（前端按此上色/分组）
CLASS = "class"
FUNCTION = "function"
SIGNAL = "signal"
ENUM = "enum"
CONST = "const"
VAR = "var"
NODE = "node"
RESOURCE = "resource"
SECTION = "section"
GROUP = "group"

# 走「符号级精确切片」的扩展名；其余（.json/.tscn 等数据文件）仍用整文件切分
STRUCTURED_CODE_EXT = {".gd", ".py"}

# ---------------------------------------------------------------- 工具

def _indent_of(line):
    s = line.lstrip(" \t")
    return len(line) - len(s)


def _strip_hash_comment(line):
    """去掉 GDScript/Python 风格行内 # 注释，字符串内的 # 保留（状态机感知引号）。"""
    out = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in ('"', "'"):
            quote = c
            triple = line[i:i + 3] == quote * 3
            if triple:
                out.append(line[i:i + 3])
                i += 3
                while i < n and line[i:i + 3] != quote * 3:
                    out.append(line[i])
                    i += 1
                if i < n:
                    out.append(line[i:i + 3])
                    i += 3
                continue
            out.append(c)
            i += 1
            while i < n and line[i] != quote:
                if line[i] == "\\":
                    out.append(line[i])
                    i += 1
                    if i < n:
                        out.append(line[i])
                        i += 1
                    continue
                out.append(line[i])
                i += 1
            if i < n:
                out.append(line[i])
                i += 1
        elif c == "#":
            break
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _clean_comment(line):
    """`## 文档` / `# 注` -> 纯文本，保留缩进层级。"""
    s = line.strip()
    if s.startswith("##"):
        s = s[2:]
    elif s.startswith("#"):
        s = s[1:]
    return s.strip()


def _truncate(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _mk(name, kind, start, end, signature="", doc="", detail="", parent=""):
    return {
        "name": name,
        "kind": kind,
        "start": start,
        "end": max(end, start),
        "parent": parent,
        "signature": _truncate(signature, 300),
        "doc": _truncate(doc, 400),
        "detail": _truncate(detail, 200),
    }


# ---------------------------------------------------------------- GDScript

_GD_CLASS_NAME = re.compile(r"^class_name\s+(\w+)\s*(?:extends\s+(.+?))?\s*$")
_GD_EXTENDS = re.compile(r"^extends\s+(.+?)\s*$")
_GD_FUNC = re.compile(
    r"^(?:static\s+|remote\s+|master\s+|puppet\s+|remotesync\s+|mastersync\s+|puppetsync\s+)*"
    r"func\s+(\w+)\s*\((.*?)\)\s*(?:->\s*([^:]+?))?\s*:\s*(.*)$"
)
_GD_INNER_CLASS = re.compile(r"^class\s+(\w+)\s*(?:extends\s+([\w.]+))?\s*:\s*$")
_GD_SIGNAL = re.compile(r"^signal\s+(\w+)\s*(?:\((.*)\))?\s*$")
_GD_ENUM_START = re.compile(r"^enum\s+(\w+)?\s*(?:\{(.*))?$")
_GD_CONST = re.compile(r"^const\s+(\w+)\s*(?::\s*([^=]+))?\s*=\s*(.*)$")
_GD_VAR = re.compile(
    r"^(?:static\s+)?var\s+(\w+)\s*(?::\s*([^=]+?))?\s*(?:=(.*))?$"
)
_GD_ANNOTATION = re.compile(r"^@([\w.]+)(?:\((.*)\))?\s*$")
_GD_EXPORT_GROUP = re.compile(r"""^@export_(?:sub)?group\(\s*['"]([^'"]+)['"]""")

_GD_LEAF_KINDS = {CONST, VAR, SIGNAL, ENUM, GROUP}


def _strip_inline_annotations(code):
    """去掉行首内联注解（@export / @onready 等），返回剩余声明代码。"""
    prev = None
    while prev != code:
        prev = code
        code = re.sub(r"^@[\w.]+\s*(?:\([^)]*\))?\s*", "", code)
    return code


def _gd_doc_above(raw_lines, idx, indent, consumed):
    """收集声明上方连续注释（同缩进），## 文档注释优先；consumed 中的行不重复用。"""
    docs = []
    j = idx - 1
    while j >= 0 and j not in consumed:
        s = raw_lines[j].strip()
        if not s.startswith("#"):
            break
        if _indent_of(raw_lines[j]) not in (indent, indent + 1):
            break
        docs.append(_clean_comment(raw_lines[j]))
        j -= 1
    docs.reverse()
    text = "\n".join(d for d in docs if d)
    return _truncate(text, 400)


def _gd_block_end(raw_lines, code_lines, i, indent):
    """缩进块（func/inner class）结束行：返回 1 基含端点行号（裁掉尾随空行）。"""
    j = i + 1
    n = len(raw_lines)
    while j < n:
        s = code_lines[j].strip()
        if s and not s.startswith("#") and _indent_of(raw_lines[j]) <= indent:
            break
        j += 1
    # 裁掉块尾的空行（split("\n") 给文件尾 \n 造的空元素也算）
    while j > i + 1 and not code_lines[j - 1].strip():
        j -= 1
    return j  # j 是块外第一行（0 基），恰好等于块内最后一行的 1 基行号


def _gd_continue_end(code_lines, i):
    """单行声明因括号未闭合/反斜杠续行时，返回结束行（0 基，含）。"""
    depth = 0
    j = i
    n = len(code_lines)
    while j < n:
        for ch in code_lines[j]:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
        cont = code_lines[j].rstrip().endswith("\\") or depth > 0
        if not cont:
            return j
        j += 1
    return n - 1


def _extract_gdscript(text):
    raw = text.split("\n")
    code = [_strip_hash_comment(ln) for ln in raw]
    n = len(raw)

    class_name, extends_ref, header_doc = "", "", ""
    symbols, consumed = [], set()

    # 头部信息：class_name / extends / 文件头 ## 文档
    first_decl = n
    for i in range(n):
        if _indent_of(raw[i]) != 0:
            continue
        s = code[i].strip()
        if not s:
            continue
        if s.startswith("@"):
            # 纯注解行（@tool/@icon）属头部；@export var 这类内联注解是首条声明
            if _GD_ANNOTATION.match(s):
                continue
            first_decl = i
            break
        m = _GD_CLASS_NAME.match(s)
        if m:
            class_name = m.group(1)
            if m.group(2):
                extends_ref = m.group(2).strip()
            continue
        m = _GD_EXTENDS.match(s)
        if m:
            extends_ref = m.group(1).strip()
            continue
        if s.startswith("#"):
            continue
        first_decl = i
        break

    # 头部文档归属（Godot 约定：## 紧贴声明上一行=该声明的文档）：
    # first_decl 正上方连续无空行的注释块归首条声明；头部区其余注释归文件/类。
    member_doc_lines = set()
    j = first_decl - 1
    while j >= 0 and raw[j].strip().startswith("#"):
        member_doc_lines.add(j)
        j -= 1

    header_docs = []
    for i in range(0, first_decl):
        if raw[i].strip().startswith("#") and i not in member_doc_lines:
            header_docs.append(_clean_comment(raw[i]))
            consumed.add(i)
    if header_docs:
        header_doc = _truncate("\n".join(d for d in header_docs if d), 400)

    def parse_region(lo, hi, parent, base_indent):
        i = lo
        while i < hi:
            line_raw, line_code = raw[i], code[i]
            ind = _indent_of(line_raw)
            s_code = line_code.strip()
            if ind != base_indent or not s_code or s_code.startswith("#"):
                i += 1
                continue

            # 独立注解行（@tool / @icon / @rpc / @export_group ...）
            ann = _GD_ANNOTATION.match(s_code)
            ann_start = i
            while ann and i < hi and _GD_ANNOTATION.match(code[i].strip()):
                gm = _GD_EXPORT_GROUP.match(code[i].strip())
                if gm:
                    symbols.append(_mk(gm.group(1), GROUP, i + 1, i + 1,
                                       signature=code[i].strip(), parent=parent))
                    consumed.add(i)
                i += 1
                consumed.add(i - 1)
                if i < hi:
                    ann = _GD_ANNOTATION.match(code[i].strip()) if _indent_of(raw[i]) == base_indent else None
                if i >= hi:
                    break
            if i >= hi:
                break
            if i != ann_start and (
                _indent_of(raw[i]) != base_indent or not code[i].strip()
            ):
                # 独立注解后没有跟声明（文件尾/空行/缩进出错），安全跳过
                i += 1
                continue
            line_raw, line_code = raw[i], code[i]
            ind = _indent_of(line_raw)
            if ind != base_indent:
                i += 1
                continue
            s_code = line_code.strip()
            if not s_code or s_code.startswith("#"):
                i += 1
                continue

            doc = _gd_doc_above(raw, ann_start, base_indent, consumed)
            start_line = ann_start + 1

            # inner class
            m = _GD_INNER_CLASS.match(s_code)
            if m:
                end = _gd_block_end(raw, code, i, ind)
                symbols.append(_mk(m.group(1), CLASS, start_line, end,
                                   signature=s_code, doc=doc, parent=parent,
                                   detail=m.group(2) or ""))
                body_indent = None
                for k in range(i + 1, end):
                    st = code[k].strip()
                    if st and not st.startswith("#"):
                        body_indent = _indent_of(raw[k])
                        break
                if body_indent is not None:
                    parse_region(i + 1, end, m.group(1), body_indent)
                i = end
                continue

            # func
            m = _GD_FUNC.match(s_code)
            if m:
                end = _gd_block_end(raw, code, i, ind)
                ret = (m.group(3) or "").strip()
                detail = ("-> " + ret) if ret else ""
                symbols.append(_mk(m.group(1), FUNCTION, start_line, end,
                                   signature=s_code, doc=doc, detail=detail,
                                   parent=parent))
                i = end
                continue

            # signal
            m = _GD_SIGNAL.match(s_code)
            if m:
                symbols.append(_mk(m.group(1), SIGNAL, start_line, i + 1,
                                   signature=s_code, doc=doc,
                                   detail=(m.group(2) or "").strip(), parent=parent))
                i += 1
                continue

            # enum（可能跨多行）
            m = _GD_ENUM_START.match(s_code)
            if m:
                end_i = i
                if "{" not in s_code or s_code.count("{") > s_code.count("}"):
                    depth = s_code.count("{") - s_code.count("}")
                    j = i + 1
                    while j < hi and depth > 0:
                        depth += code[j].count("{") - code[j].count("}")
                        end_i = j
                        j += 1
                body = "\n".join(code[k] for k in range(i, end_i + 1))
                mb = re.search(r"\{(.*)\}", body, re.S)
                members = []
                if mb:
                    for part in mb.group(1).split(","):
                        nm = part.strip().split("=")[0].strip()
                        if nm:
                            members.append(nm)
                symbols.append(_mk(m.group(1) or "(anonymous)", ENUM, start_line,
                                   end_i + 1, signature=s_code, doc=doc,
                                   detail=", ".join(members), parent=parent))
                i = end_i + 1
                continue

            # const
            m = _GD_CONST.match(s_code)
            if m:
                end_i = _gd_continue_end(code, i)
                detail_parts = [p for p in ((m.group(2) or "").strip(), (m.group(3) or "").strip()) if p]
                symbols.append(_mk(m.group(1), CONST, start_line, end_i + 1,
                                   signature=s_code, doc=doc,
                                   detail=" = ".join(detail_parts)[:200], parent=parent))
                i = end_i + 1
                continue

            # var（去掉行首 @export/@onready 等内联注解再匹配）
            # := 推断写法归一化成占位类型，便于同一正则处理（var x := v → var x : ␣ = v）
            stripped_decl = _strip_inline_annotations(s_code).replace(":=", ": ␣ =", 1)
            m = _GD_VAR.match(stripped_decl)
            if m:
                end_i = _gd_continue_end(code, i)
                detail = (m.group(2) or "").strip()
                if detail == "␣":
                    detail = ""
                symbols.append(_mk(m.group(1), VAR, start_line, end_i + 1,
                                   signature=s_code, doc=doc, detail=detail,
                                   parent=parent))
                i = end_i + 1
                continue

            i += 1

    parse_region(0, n, "", 0)
    symbols.sort(key=lambda s: (s["start"], 0 if s["parent"] == "" else 1))

    return {
        "lang": "gdscript",
        "class_name": class_name,
        "extends": extends_ref,
        "doc": header_doc,
        "symbols": symbols,
    }


# ---------------------------------------------------------------- Python

def _py_signature(node):
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    head = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    ret = ""
    if node.returns:
        try:
            ret = " -> " + ast.unparse(node.returns)
        except Exception:
            pass
    return _truncate(f"{head}{node.name}({args}){ret}:", 300)


def _extract_python(text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    symbols = []

    def doc_of(node):
        d = ast.get_docstring(node)
        return _truncate(d or "", 400)

    def start_of(node):
        if node.decorator_list:
            return min(d.lineno for d in node.decorator_list)
        return node.lineno

    def emit_func(node, parent=""):
        symbols.append(_mk(node.name, FUNCTION, start_of(node),
                           node.end_lineno or node.lineno,
                           signature=_py_signature(node), doc=doc_of(node),
                           parent=parent))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))
                except Exception:
                    pass
            symbols.append(_mk(node.name, CLASS, start_of(node),
                               node.end_lineno or node.lineno,
                               signature=f"class {node.name}:", doc=doc_of(node),
                               detail=", ".join(bases)))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    emit_func(child, node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emit_func(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    kind = CONST if t.id.isupper() else VAR
                    symbols.append(_mk(t.id, kind, node.lineno,
                                       node.end_lineno or node.lineno,
                                       parent=""))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            kind = CONST if node.target.id.isupper() else VAR
            symbols.append(_mk(node.target.id, kind, node.lineno,
                               node.end_lineno or node.lineno, parent=""))

    symbols.sort(key=lambda s: s["start"])
    return {
        "lang": "python",
        "class_name": "",
        "extends": "",
        "doc": _truncate(ast.get_docstring(tree) or "", 400),
        "symbols": symbols,
    }


# ---------------------------------------------------------------- Godot 场景/资源

_TSCN_NODE = re.compile(r'^\[node\s+name="([^"]*)"(?:\s+type="([^"]*)")?(?:\s+parent="([^"]*)")?')
_TSCN_EXT = re.compile(r'^\[ext_resource\s+(.*?)\]\s*$')
_TSCN_SUB = re.compile(r'^\[sub_resource\s+(.*?)\]\s*$')
_ATTR_TYPE = re.compile(r'type="([^"]*)"')
_ATTR_PATH = re.compile(r'path="([^"]*)"')
_ATTR_ID = re.compile(r'id="([^"]*)"')
_INI_SECTION = re.compile(r"^\[([^\]]+)\]$")


def _extract_godot_scene(text, lang):
    """抽取 .tscn 的 node 树与 .tres/.godot 的 resource/section 段。"""
    lines = text.split("\n")
    symbols = []
    section_start = None

    def flush(end_line):
        if section_start is not None:
            symbols[-1]["end"] = end_line

    for i, ln in enumerate(lines):
        s = ln.strip()
        m = _TSCN_NODE.match(s)
        if m:
            if symbols:
                symbols[-1]["end"] = i
            parent = m.group(3) or "."
            symbols.append(_mk(m.group(1), NODE, i + 1, i + 1,
                               signature=s, detail=(m.group(2) or ""),
                               parent="" if parent == "." else parent))
            continue
        m = _TSCN_EXT.match(s)
        if m:
            if symbols:
                symbols[-1]["end"] = i
            attrs = m.group(1)
            mt, mp = _ATTR_TYPE.search(attrs), _ATTR_PATH.search(attrs)
            typ = mt.group(1) if mt else ""
            path = mp.group(1) if mp else ""
            symbols.append(_mk(os.path.basename(path) or "(external)", RESOURCE,
                               i + 1, i + 1, signature=s, detail=typ))
            continue
        m = _TSCN_SUB.match(s)
        if m:
            if symbols:
                symbols[-1]["end"] = i
            attrs = m.group(1)
            mt, mi = _ATTR_TYPE.search(attrs), _ATTR_ID.search(attrs)
            typ = mt.group(1) if mt else ""
            sid = mi.group(1) if mi else "(sub)"
            symbols.append(_mk(sid, RESOURCE, i + 1, i + 1, signature=s, detail=typ))
            continue
        m = _INI_SECTION.match(s)
        if m and lang != "godot-scene":
            if symbols:
                symbols[-1]["end"] = i
            symbols.append(_mk(m.group(1), SECTION, i + 1, i + 1, signature=s))
    if symbols:
        symbols[-1]["end"] = len(lines)
    return {
        "lang": lang,
        "class_name": "",
        "extends": "",
        "doc": "",
        "symbols": symbols,
    }


# ---------------------------------------------------------------- Java
# 正则启发式（非完整语法分析）：先掩码注释/字符串字面量，再按花括号深度定位
# 类型体；成员签名允许跨行，方法体从首个 { 做括号配平定结束行。
# 匿名类/方法内部的局部声明因深度更深而不会被误收为成员。

_JAVA_MOD_PART = (
    r"(?:public|private|protected|static|final|abstract|native|synchronized|"
    r"strictfp|default|sealed|non-sealed|transient|volatile)\s+"
)
_JAVA_TYPE_HEAD = re.compile(
    r"^(?P<mods>(?:" + _JAVA_MOD_PART + r")*)"
    r"(?P<kind>class|interface|enum|record|@interface)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
)
_JAVA_TYPE_EXPR = r"@?[\w$.]+(?:\s*<[^;{}]*>)?(?:\s*\[\s*\])*(?:\s*\.\s*\.\s*\.)?"
_JAVA_METHOD_HEAD = re.compile(
    r"^(?P<mods>(?:" + _JAVA_MOD_PART + r")*)"
    r"(?:<[^;{}<>]*(?:<[^;{}<>]*>[^;{}<>]*)*>\s*)?"          # 泛型方法 <T extends …>
    r"(?P<ret>" + _JAVA_TYPE_EXPR + r")\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"\((?P<params>[^;{}]*)\)\s*"
    r"(?:throws\s+[\w$.,\s]+?\s*)?"
    r"(?:default\b[^;{}]*)?"
    r"(?P<end>[;{])\s*$"
)
_JAVA_FIELD_HEAD = re.compile(
    r"^(?P<mods>(?:" + _JAVA_MOD_PART + r")*)"
    r"(?P<type>" + _JAVA_TYPE_EXPR + r")\s+"
    r"(?P<rest>[\w$].*)$"
)
_JAVA_ENUM_CONST = re.compile(r"^([A-Z][\w$]*)\b")


def _java_mask(text):
    """把 // 、/* */ 注释与字符串/字符字面量内容替换为空白（保留换行与制表符），
    使花括号深度计数不受注释/字面量中的括号干扰。"""
    out = []
    i, n = 0, len(text)
    state = "code"
    while i < n:
        c = text[i]
        if state == "code":
            if text[i:i + 2] == "//":
                state = "line"; out.append("  "); i += 2
            elif text[i:i + 2] == "/*":
                state = "block"; out.append("  "); i += 2
            elif c == '"':
                state = "str"; out.append(" "); i += 1
            elif c == "'":
                state = "char"; out.append(" "); i += 1
            else:
                out.append(c); i += 1
        elif state == "line":
            out.append("\n" if c == "\n" else ("\t" if c == "\t" else " "))
            if c == "\n":
                state = "code"
            i += 1
        elif state == "block":
            if text[i:i + 2] == "*/":
                state = "code"; out.append("  "); i += 2
            else:
                out.append("\n" if c == "\n" else ("\t" if c == "\t" else " ")); i += 1
        else:  # str / char
            q = '"' if state == "str" else "'"
            if c == "\\":
                out.append(" ")
                if i + 1 < n:
                    nxt = text[i + 1]
                    out.append("\n" if nxt == "\n" else " ")
                    i += 2
                else:
                    i += 1
            elif c == q:
                state = "code"; out.append(" "); i += 1
            else:
                out.append("\n" if c == "\n" else ("\t" if c == "\t" else " ")); i += 1
    return "".join(out)


def _java_javadoc_ends(text):
    """返回 {结束行(0基): 清洗后的 javadoc 文本}；非 javadoc 的普通块注释不收录。"""
    ends = {}
    for m in re.finditer(r"/\*\*(.*?)\*/", text, re.S):
        end_line = text.count("\n", 0, m.end())
        cleaned = []
        for ln in m.group(1).split("\n"):
            s = ln.strip()
            if s.startswith("*"):
                s = s[1:].lstrip("*").strip()
            if s:
                cleaned.append(s)
        if cleaned:
            ends[end_line] = "\n".join(cleaned)[:400]
    return ends


def _brace_span_end(lines, open_idx, start_depth):
    """从含首个 { 的 open_idx 行起做花括号配平，返回闭合行（0基）；找不到返回 open_idx。"""
    depth = 0
    for j in range(open_idx, min(len(lines), open_idx + 4000)):
        depth += lines[j].count("{") - lines[j].count("}")
        if depth <= 0:
            return j
    return open_idx


def _extract_java(text):
    lines = _java_mask(text).split("\n")
    n = len(lines)
    jdoc = _java_javadoc_ends(text)
    symbols = []
    depth = 0
    # 栈项：{name, kind, body_depth, end0, consts_done}
    stack = []
    primary = None  # (name, extends, is_public)
    ann_start = -1  # 待消费注解的最早行（0基），-1 表示无
    i = 0

    def take_doc(decl_line):
        d_line = ann_start if ann_start >= 0 else decl_line
        return jdoc.get(d_line - 1, "")

    while i < n:
        s = lines[i].strip()
        same_line_tail = False  # 本行注解被剥掉、s 为剩余声明
        if not s:
            i += 1
            continue
        top = stack[-1] if stack else None
        in_body = bool(top) and depth == top["body_depth"]

        # —— 注解行（仅在类型体内收集，参数允许跨行）——
        if in_body and s.startswith("@"):
            if ann_start < 0:
                ann_start = i
            # 跨过注解名与（可能跨行、嵌套的）参数括号
            col = 1
            while col < len(s) and (s[col].isalnum() or s[col] in "$_."):
                col += 1
            while col < len(s) and s[col] in " \t":
                col += 1
            li = i
            if col < len(s) and s[col] == "(":
                bal = 1
                col += 1
                while bal > 0:
                    if li == i:
                        if col >= len(s):
                            li += 1; col = 0
                            continue
                        ch = s[col]; col += 1
                    else:
                        ln = lines[li]
                        if col >= len(ln):
                            li += 1; col = 0
                            continue
                        ch = ln[col]; col += 1
                    if ch == "(":
                        bal += 1
                    elif ch == ")":
                        bal -= 1
            if li == i:
                tail = s[col:].strip()
                if not tail:
                    i += 1
                    continue
                s = tail  # 注解与声明同行：用剩余文本继续本行分类
                same_line_tail = True
            else:
                rest_line = lines[li][col:].strip()
                i = li if rest_line else li + 1
                continue

        # —— 枚举常量区（第一个 ; 之前）——
        if in_body and top["kind"] == "enum" and not top["consts_done"]:
            mc1 = _JAVA_ENUM_CONST.match(s)
            if mc1 and not s.startswith(";"):
                end0 = i
                if "{" in s:
                    end0 = _brace_span_end(lines, i, depth)
                symbols.append(_mk(mc1.group(1), CONST, i + 1, end0 + 1,
                                   parent=top["name"]))
                if ";" in s:
                    top["consts_done"] = True
                depth += lines[i].count("{") - lines[i].count("}")
                i += 1
                continue
            # 常量参数跨行延续、匿名常量体闭合行（};）等
            if ";" in s:
                top["consts_done"] = True
            depth += lines[i].count("{") - lines[i].count("}")
            while stack and depth < stack[-1]["body_depth"]:
                stack.pop()
            i += 1
            continue

        # —— 类型声明（含嵌套类型）——
        m = _JAVA_TYPE_HEAD.match(s)
        if m and (depth == 0 or in_body):
            # 收集到首个 { 为止的完整头部
            k = i
            joined = s
            while "{" not in joined and k + 1 < n and k - i < 20:
                k += 1
                joined += " " + lines[k].strip()
            if "{" in joined:
                open_idx = k
                for jj in range(i, k + 1):
                    if "{" in lines[jj]:
                        open_idx = jj
                        break
                end0 = _brace_span_end(lines, open_idx, depth)
                name = m.group("name")
                raw_kind = m.group("kind")  # class/interface/enum/record/@interface
                ext = ""
                me = re.search(r"\bextends\s+([\w$]+)", joined.split("{", 1)[0])
                if me:
                    ext = me.group(1)
                sym = _mk(name, ENUM if raw_kind == "enum" else CLASS,
                          (ann_start if ann_start >= 0 else i) + 1, end0 + 1,
                          signature=joined.split("{", 1)[0].strip()[:300],
                          doc=take_doc(i), parent=top["name"] if top else "")
                symbols.append(sym)
                body_depth = depth
                for jj in range(i, open_idx + 1):
                    body_depth += lines[jj].count("{") - lines[jj].count("}")
                stack.append({
                    "name": name, "kind": raw_kind, "body_depth": body_depth,
                    "end0": end0, "consts_done": False,
                })
                if depth == 0:
                    is_pub = "public" in m.group("mods")
                    if primary is None or (is_pub and not primary[2]):
                        primary = (name, ext, is_pub)
                ann_start = -1
                if body_depth == depth:
                    stack.pop()  # class X {} 类体在头部行已自闭合
                depth = body_depth  # 头部可能跨行：直接定位到类型体内深度
                i = open_idx + 1
                continue

        # —— 静态/实例初始化块：不出符号，靠深度计数跳过 ——
        if in_body and (s == "{" or s.startswith("{") or re.match(r"^static\s*\{", s)):
            ann_start = -1
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
            continue

        if in_body and not s.startswith("}"):
            # 同行注解已被剥掉时，扫描用行以剥后文本替代
            slines = list(lines) if same_line_tail else lines
            if same_line_tail:
                slines[i] = s

            def scan_end(want_brace):
                """从 i 起找成员结束：want_brace=True 取首个 { 的配平闭合行；
                want_brace=False 取花括号深度归零且含 ; 的语句行。返回 (行号, token)。"""
                run2, opened = 0, None
                jj = i
                while jj < n and jj - i < 80:
                    lj = slines[jj]
                    run2 += lj.count("{") - lj.count("}")
                    if want_brace and opened is None and "{" in lj:
                        opened = jj
                    if opened is None and run2 == 0 and ";" in lj:
                        return jj, ";"
                    if want_brace and opened is not None and run2 <= 0:
                        return jj, "{"
                    jj += 1
                return jj, None

            def build_sig(last_j, tok):
                parts = []
                open_line = None
                if tok == "{":
                    for jj in range(i, last_j + 1):
                        if "{" in slines[jj]:
                            open_line = jj
                            break
                for jj in range(i, last_j + 1):
                    part = slines[jj].strip()
                    if tok == "{":
                        if jj == open_line:
                            part = part.split("{", 1)[0].strip()
                        elif jj > open_line:
                            part = ""
                    if part:
                        parts.append(part)
                base = " ".join(parts).strip()
                if tok == ";":
                    base = base.rstrip(";").strip()  # 行内自带分号，避免重复
                return (base + (" " + tok if tok else "")).strip()

            end_j, tok = scan_end(True)
            if tok is None:
                end_j, tok = scan_end(False)
            if tok is None:
                # 无法判定结束位置（畸形/不支持的写法）：只做深度维护，不产出符号
                depth += lines[i].count("{") - lines[i].count("}")
                while stack and depth < stack[-1]["body_depth"]:
                    stack.pop()
                i += 1
                continue
            sig = build_sig(end_j, tok)

            ctor = re.compile(
                r"^(?P<mods>(?:" + _JAVA_MOD_PART + r")*)"
                + re.escape(top["name"]) + r"\s*\((?P<params>[^;{}]*)\)\s*"
                r"(?:throws\s+[\w$.,\s]+?)?(?P<end>[;{])\s*$"
            )
            mc = ctor.match(sig)
            mm = _JAVA_METHOD_HEAD.match(sig) if not mc else None
            if mc or mm:
                if mc:
                    mname, detail = top["name"], "constructor"
                else:
                    mname, detail = mm.group("name"), (mm.group("ret").strip() or "")
                symbols.append(_mk(
                    mname, FUNCTION, (ann_start if ann_start >= 0 else i) + 1, end_j + 1,
                    signature=sig[:300], doc=take_doc(i),
                    detail=detail, parent=top["name"],
                ))
                ann_start = -1
                i = end_j + 1  # 整段花括号配平、净深度不变，跳过方法体
                continue

            # 非方法：按分号语句重扫（兼容字段初始化里的 lambda/匿名类花括号）
            fend, ftok = scan_end(False)
            fsig = build_sig(fend, ftok)
            mf = _JAVA_FIELD_HEAD.match(fsig.rstrip(";").strip())
            head_m = None
            following = "("
            if mf:
                head_m = re.match(r"([\w$]+)", mf.group("rest"))
                if head_m:
                    following = mf.group("rest")[head_m.end():].lstrip()
            if mf and ftok == ";" and head_m and (not following or following[0] in "=,["):
                mods = mf.group("mods")
                is_const = ("static" in mods and "final" in mods) or top["kind"] in ("interface", "@interface")
                names = [head_m.group(1)]
                tail = following
                if tail.startswith("=") or "=" in tail:
                    tail = tail.split("=", 1)[0]
                depth_p = 0
                buf = ""
                for ch in tail:
                    if ch in "(<[":
                        depth_p += 1
                    elif ch in ")>]":
                        depth_p -= 1
                    elif ch == "," and depth_p == 0:
                        nm2 = re.match(r"\s*([\w$]+)", buf)
                        if nm2:
                            names.append(nm2.group(1))
                        buf = ""
                        continue
                    buf += ch
                for nm in names:
                    symbols.append(_mk(
                        nm, CONST if is_const else VAR,
                        (ann_start if ann_start >= 0 else i) + 1, fend + 1,
                        doc=take_doc(i) if nm == names[0] else "",
                        detail=mf.group("type").strip(), parent=top["name"],
                    ))
                ann_start = -1
                i = fend + 1  # 整条语句（含初始化花括号）净深度不变
                continue

        ann_start = -1
        depth += lines[i].count("{") - lines[i].count("}")
        # 枚举匿名常量体的闭合行（};）从底路径返回：标记常量区结束
        if (stack and stack[-1]["kind"] == "enum"
                and not stack[-1]["consts_done"]
                and depth == stack[-1]["body_depth"] and ";" in lines[i]):
            stack[-1]["consts_done"] = True
        # 闭合已记录结束行的类型
        while stack and depth < stack[-1]["body_depth"]:
            stack.pop()
        i += 1

    symbols.sort(key=lambda s: s["start"])
    cname, extends = "", ""
    if primary:
        cname, extends = primary[0], primary[1]
    return {"lang": "java", "class_name": cname, "extends": extends,
            "doc": "", "symbols": symbols}


# ---------------------------------------------------------------- 通用兜底

_GENERIC_PATTERNS = [
    (re.compile(r"\b(?:function|func(?:tion)?|fn|def)\s+(\w+(?:[.:]\w+)*)"), FUNCTION),
    (re.compile(r"\b(?:class|struct|interface|trait|impl|module|namespace)\s+([A-Za-z_]\w*)"), CLASS),
    (re.compile(r"\benum\s+(\w+)"), ENUM),
    (re.compile(r"\b(?:pub\s+)?const\s+(\w+)"), CONST),
    (re.compile(r"\b(?:var|let|val|uniform|varying)\s+(\w+)"), VAR),
]


def _extract_generic(text, lang):
    lines = text.split("\n")
    symbols = []
    in_block_comment = False
    for i, ln in enumerate(lines):
        code = ln
        if "/*" in code:
            code = re.sub(r"/\*.*?\*/", "", code)
            if "/*" in code and "*/" not in code.split("/*", 1)[1]:
                in_block_comment = True
                code = code.split("/*")[0]
        elif in_block_comment:
            if "*/" in code:
                in_block_comment = False
                code = code.split("*/", 1)[1]
            else:
                continue
        code = re.sub(r"//.*$", "", code)
        s = code.strip()
        if not s:
            continue
        for pat, kind in _GENERIC_PATTERNS:
            m = pat.search(s)
            if m:
                # Lua 形态 function M.foo() / A:bar() 取末段符号名
                rawname = re.split(r"[.:]", m.group(1))[-1]
                end = i + 1
                # 大括号语言：从声明行配平到闭合括号（单行声明立即闭合）
                if "{" in code and kind in (FUNCTION, CLASS, ENUM):
                    depth = 0
                    for j in range(i, min(len(lines), i + 2000)):
                        depth += lines[j].count("{") - lines[j].count("}")
                        if depth <= 0:
                            end = j + 1
                            break
                elif lang == "lua":
                    # Lua: function ... end
                    for j in range(i, min(len(lines), i + 400)):
                        if re.search(r"(^|\s)end(\s|$|;)", lines[j]):
                            end = j + 1
                            break
                symbols.append(_mk(rawname, kind, i + 1, end,
                                   signature=s[:300]))
                break
    symbols.sort(key=lambda s: s["start"])
    return {"lang": lang, "class_name": "", "extends": "", "doc": "", "symbols": symbols}


# ---------------------------------------------------------------- 分发 + 缓存

_LANG_BY_EXT = {
    ".gd": "gdscript", ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".cs": "csharp", ".java": "java",
    ".cpp": "cpp", ".cc": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp",
    ".go": "go", ".rs": "rust", ".lua": "lua", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".kt": "kotlin", ".scala": "scala", ".sh": "shell",
    ".gdshader": "gdshader", ".tscn": "godot-scene",
    ".tres": "godot-resource", ".godot": "ini",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
}

# 不做行级大纲的数据文件（地图视图仍展示文件名，不展开符号）
_DATA_EXT = {".json", ".yaml", ".yml", ".toml", ".godot"}

_cache = {}


def extract(text, path):
    """按扩展名解析文本，返回符号信封；解析器异常时退回空信封，绝不阻断调用方。"""
    ext = os.path.splitext(path)[1].lower()
    lang = _LANG_BY_EXT.get(ext, "text")
    empty = {"lang": lang, "class_name": "", "extends": "", "doc": "", "symbols": []}
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        if ext == ".gd":
            return _extract_gdscript(text)
        if ext == ".py":
            return _extract_python(text) or empty
        if ext == ".java":
            return _extract_java(text)
        if ext == ".tscn":
            return _extract_godot_scene(text, "godot-scene")
        if ext == ".tres":
            return _extract_godot_scene(text, "godot-resource")
        if ext in _DATA_EXT:
            return empty
        return _extract_generic(text, lang)
    except Exception as e:  # 解析器 bug 不应影响大纲/索引主流程
        print(f"[symbols] 解析失败 {path}: {e}")
        return empty


def file_symbols(abs_path):
    """读盘 + (mtime_ns, size) 缓存。"""
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(abs_path)), st.st_mtime_ns, st.st_size)
    cached = _cache.get(key[0])
    if cached and cached[0] == key:
        return cached[1]
    try:
        # utf-8-sig：Godot 编辑器默认写 UTF-8 BOM，不带 BOM 的文件同样可读
        with open(abs_path, encoding="utf-8-sig", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    if text.startswith("\ufeff"):
        text = text[1:]
    env = extract(text, abs_path)
    _cache[key[0]] = (key, env)
    return env
