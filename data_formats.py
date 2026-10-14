"""Project-scoped file recognition, dirty-data normalization and data generation.

The module is intentionally dependency-light. Optional readers (PyYAML and
Office packages already used by the project) are loaded only for the matching
format. All paths passed to the writer are resolved below the active project
root and writes are atomic.
"""
from __future__ import annotations

import csv
import base64
import binascii
import datetime as _dt
import hashlib
import io
import json
import math
import mimetypes
import os
import re
import tempfile
import tomllib
import unicodedata
import xml.etree.ElementTree as ET
import logging
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping


logger = logging.getLogger(__name__)


MAX_READ_BYTES = 32 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 512
MAX_OUTPUT_BYTES = 64 * 1024 * 1024

FORMAT_EXTENSIONS = {
    ".txt": "text", ".md": "markdown", ".markdown": "markdown",
    ".csv": "csv", ".tsv": "tsv", ".json": "json", ".jsonl": "jsonl",
    ".ndjson": "jsonl", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".html": "html", ".htm": "html", ".pdf": "pdf",
    ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".zip": "archive", ".7z": "archive",
    ".bin": "binary", ".scn": "godot_binary_scene",
    ".tscn": "text", ".tres": "text", ".gd": "text", ".cfg": "text",
}

TEXT_FILENAMES = {".gitignore", ".gitattributes", ".editorconfig", ".npmrc", ".prettierrc"}

_MAGIC = (
    (b"%PDF-", "pdf"), (b"PK\x03\x04", "office_or_archive"),
    (b"\x89PNG\r\n\x1a\n", "image"), (b"\xff\xd8\xff", "image"),
    (b"GIF8", "image"), (b"RIFF", "riff"),
)
_EMPTY_MARKERS = {"", "-", "—", "n/a", "na", "null", "none", "nil", "未知", "无"}
_BOOL_TRUE = {"true", "yes", "y", "是", "对"}
_BOOL_FALSE = {"false", "no", "n", "否", "错"}


class DataFormatError(ValueError):
    pass


def _bounded_bytes(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise DataFormatError(f"文件超过读取上限（{MAX_READ_BYTES // 1024 // 1024} MB）")
    return path.read_bytes()


def _magic_format(raw: bytes) -> str:
    for prefix, kind in _MAGIC:
        if raw.startswith(prefix):
            if kind == "office_or_archive":
                # OOXML containers have distinguishable directory entries.
                sample = raw[:2 * 1024 * 1024]
                if b"word/" in sample:
                    return "docx"
                if b"xl/" in sample:
                    return "xlsx"
                if b"ppt/" in sample:
                    return "pptx"
                return "archive"
            return kind
    return ""


def _decode(raw: bytes) -> tuple[str, str, float]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-sig", 1.0
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace"), "utf-16", 1.0
    # UTF-16 文件不一定带 BOM。NUL 字节交替出现是一个足够可靠的
    # 轻量信号，先尝试 UTF-16，再回到常见中文/西文编码。
    candidates = ("utf-16", "utf-8", "gb18030", "cp1252", "latin-1") if (
        b"\x00" in raw[:4096]
    ) else ("utf-8", "gb18030", "cp1252", "latin-1")
    scored: list[tuple[float, str, str]] = []
    for encoding in candidates:
        text = raw.decode(encoding, errors="replace")
        replacement = text.count("\ufffd")
        controls = sum(1 for char in text if unicodedata.category(char) == "Cc" and char not in "\n\r\t")
        # 无 BOM 的 UTF-16 用错误编码解码时会产生大量控制字符；把它
        # 作为强惩罚，避免 latin-1 因“永不报错”抢走候选。
        score = replacement * 20 + controls * 2
        if encoding == "utf-16" and b"\x00" not in raw[:4096]:
            score += 10
        scored.append((score, encoding, text))
    score, encoding, text = min(scored, key=lambda row: row[0])
    confidence = max(0.0, min(1.0, 1.0 - score / max(1, len(text) * 2)))
    return text, encoding, confidence


def _guess_text_format(text: str) -> str:
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        return "text"
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            if "\n" in stripped:
                return "jsonl"
    if stripped.startswith("<"):
        if re.search(r"<html(?:\s|>)|<!doctype\s+html", stripped[:500], re.I):
            return "html"
        try:
            ET.fromstring(stripped)
            return "xml"
        except ET.ParseError:
            pass
    lines = [line for line in stripped.splitlines()[:10] if line.strip()]
    if lines and any(delimiter in lines[0] for delimiter in (",", "\t", ";", "|")):
        try:
            csv.Sniffer().sniff("\n".join(lines), delimiters=",\t;|")
            return "csv"
        except csv.Error:
            pass
    return "text"


def detect_file(path: str | os.PathLike[str], *, read_bytes: bool = True) -> dict[str, Any]:
    """Identify a file using both extension and magic bytes."""
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise DataFormatError("文件不存在")
    raw = _bounded_bytes(target) if read_bytes else b""
    ext = target.suffix.lower()
    extension_format = "text" if target.name.lower() in TEXT_FILENAMES else FORMAT_EXTENSIONS.get(ext, "unknown")
    magic = _magic_format(raw) if raw else ""
    if magic == "office_or_archive":
        magic = "archive"
    fmt = magic if magic in {"pdf", "docx", "xlsx", "pptx", "image", "archive"} else extension_format
    if fmt == "office_or_archive":
        fmt = "archive"
    text_like = fmt in {"text", "markdown", "csv", "tsv", "json", "jsonl", "yaml", "toml", "xml", "html"}
    encoding = "binary"
    confidence = 0.98 if magic else (0.85 if extension_format != "unknown" else 0.2)
    if text_like and raw:
        _, encoding, decode_confidence = _decode(raw)
        confidence = min(confidence, decode_confidence)
    elif fmt == "unknown" and raw:
        sample = raw[:4096]
        even_nuls = sample[0::2].count(0) / max(1, len(sample[0::2]))
        odd_nuls = sample[1::2].count(0) / max(1, len(sample[1::2]))
        utf16_pattern = (max(even_nuls, odd_nuls) > 0.3 and min(even_nuls, odd_nuls) < 0.05)
        if b"\x00" in sample and not (utf16_pattern or sample.startswith((b"\xff\xfe", b"\xfe\xff"))):
            fmt = "binary"
        else:
            decoded, encoding, decode_confidence = _decode(raw)
            guessed = _guess_text_format(decoded)
            controls = sum(1 for char in decoded[:4096] if unicodedata.category(char) == "Cc" and char not in "\n\r\t")
            if controls <= max(1, len(decoded[:4096]) // 100) and decode_confidence >= 0.8:
                fmt, text_like, confidence = guessed, True, round(decode_confidence * 0.75, 3)
    return {
        "path": str(target), "name": target.name, "extension": ext,
        "format": fmt, "mime": mimetypes.guess_type(str(target))[0] or "application/octet-stream",
        "bytes": len(raw) if raw else target.stat().st_size, "encoding": encoding,
        "confidence": round(confidence, 3), "binary": not text_like,
        "magic": magic or None,
    }


def _clean_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _header(value: Any, index: int) -> str:
    raw = _clean_text(str(value if value is not None else ""))
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or f"column_{index + 1}"


def _unique_headers(values: Iterable[Any]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for index, value in enumerate(values):
        base = _header(value, index)
        count = seen.get(base, 0) + 1
        seen[base] = count
        out.append(base if count == 1 else f"{base}_{count}")
    return out[:MAX_COLUMNS]


def _scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    text = re.sub(r"\s+", " ", _clean_text(str(value))).strip()
    if text.lower() in _EMPTY_MARKERS:
        return None
    lowered = text.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    number = text.replace(",", "")
    if re.fullmatch(r"[-+]?\d+", number):
        try:
            return int(number)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", number):
        try:
            value = float(number)
            return value if math.isfinite(value) else text
        except ValueError:
            pass
    return text


def normalize_table(headers: Iterable[Any], rows: Iterable[Iterable[Any]], *, source: str = "") -> dict[str, Any]:
    header_values_raw = list(headers or [])
    raw_rows = [list(row) if isinstance(row, (list, tuple)) else [row] for row in rows]
    raw_rows = raw_rows[:MAX_ROWS]
    width = min(MAX_COLUMNS, max([len(header_values_raw)] + [len(row) for row in raw_rows] + [0]))
    header_values = header_values_raw[:width]
    headers_out = _unique_headers(header_values + [""] * max(0, width - len(header_values)))
    rows_out: list[list[Any]] = []
    for row in raw_rows:
        cells = [_scalar(cell) for cell in row[:width]]
        cells.extend([None] * (width - len(cells)))
        rows_out.append(cells)
    return {"kind": "table", "source": source, "headers": headers_out,
            "rows": rows_out, "row_count": len(rows_out), "column_count": width}


def _parse_delimited(text: str, delimiter: str | None = None) -> dict[str, Any]:
    sample = text[:64_000]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            delimiter = "\t" if "\t" in sample and sample.count("\t") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True))
    rows = [[cell.strip() for cell in row] for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return normalize_table([], [], source="delimited")
    first = rows[0]
    # Treat a row as headers when it contains unique non-numeric labels.
    non_numeric = sum(1 for cell in first if cell.strip() and
                      not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cell.strip()))
    header_like = bool(first) and non_numeric >= max(1, len(first) // 2 + 1)
    return normalize_table(first if header_like else [], rows[1:] if header_like else rows, source="delimited")


def _json_table(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value and all(isinstance(item, Mapping) for item in value):
        keys: list[str] = []
        for item in value:
            for key in item:
                if str(key) not in keys:
                    keys.append(str(key))
        return normalize_table(keys, [[item.get(key) for key in keys] for item in value], source="json")
    if isinstance(value, Mapping):
        for key in ("rows", "records", "items", "data"):
            candidate = value.get(key)
            parsed = _json_table(candidate)
            if parsed:
                parsed["source"] = "json." + key
                return parsed
    return None


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def read_file(path: str | os.PathLike[str], *, normalize: bool = True) -> dict[str, Any]:
    """Read supported formats into a bounded, inspectable representation."""
    target = Path(path).expanduser().resolve()
    info = detect_file(target)
    fmt = info["format"]
    raw = _bounded_bytes(target)
    if fmt in {"text", "markdown", "csv", "tsv", "json", "jsonl", "yaml", "toml", "xml", "html"}:
        text, encoding, confidence = _decode(raw)
        info.update({"encoding": encoding, "encoding_confidence": round(confidence, 3)})
        if fmt in {"text", "markdown"}:
            return {**info, "kind": "text", "text": _clean_text(text)}
        if fmt in {"csv", "tsv"}:
            table = _parse_delimited(text, "\t" if fmt == "tsv" else None)
            return {**info, **table}
        if fmt == "jsonl":
            values = []
            errors = []
            for line_no, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    values.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    errors.append({"line": line_no, "error": str(exc)})
            table = _json_table(values)
            return {**info, **(table or {}), "kind": "records", "records": values[:MAX_ROWS], "errors": errors[:50]}
        if fmt == "json":
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                # 脏 JSON 仍然要进入摄取/检查流程：保留原文预览和
                # 定位信息，让 Agent 能决定修复或转成 JSONL。
                return {**info, "kind": "json", "value": None,
                        "text": _clean_text(text)[:16000],
                        "errors": [{"line": exc.lineno, "column": exc.colno,
                                    "error": exc.msg}]}
            table = _json_table(value)
            return {**info, "kind": "json", "value": value, **(table or {})}
        if fmt == "yaml":
            try:
                import yaml  # type: ignore
                value = yaml.safe_load(text)
            except ImportError:
                return {**info, "kind": "text", "text": _clean_text(text), "warning": "未安装 PyYAML，保留原文"}
            except Exception as exc:
                return {**info, "kind": "yaml", "value": None,
                        "text": _clean_text(text)[:16000],
                        "errors": [{"error": str(exc)}]}
            table = _json_table(value)
            return {**info, "kind": "yaml", "value": value, **(table or {})}
        if fmt == "toml":
            try:
                value = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                return {**info, "kind": "toml", "value": None,
                        "text": _clean_text(text)[:16000],
                        "errors": [{"error": str(exc)}]}
            return {**info, "kind": "toml", "value": value, **(_json_table(value) or {})}
        if fmt == "xml":
            try:
                root = ET.fromstring(text)
            except ET.ParseError as exc:
                return {**info, "kind": "xml", "root": None,
                        "text": _clean_text(text)[:16000],
                        "errors": [{"error": str(exc)}]}
            rows = []
            for node in list(root)[:MAX_ROWS]:
                rows.append({child.tag: _clean_text(child.text or "") for child in list(node)})
            return {**info, "kind": "xml", "root": root.tag, "value": rows if rows else _clean_text(text)}
        parser = _TextExtractor()
        parser.feed(text)
        return {**info, "kind": "text", "text": _clean_text("\n".join(parser.parts))}
    if fmt == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(target))
        return {**info, "kind": "text", "text": _clean_text("\n".join(page.extract_text() or "" for page in reader.pages))}
    if fmt == "docx":
        from docx import Document
        doc = Document(str(target))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            parts.extend("\t".join(cell.text for cell in row.cells) for row in table.rows)
        return {**info, "kind": "text", "text": _clean_text("\n".join(parts))}
    if fmt == "xlsx":
        from openpyxl import load_workbook
        workbook = load_workbook(str(target), read_only=True, data_only=True)
        sheets = {}
        for sheet in workbook.worksheets:
            rows = list(sheet.iter_rows(values_only=True))[:MAX_ROWS]
            sheets[sheet.title] = normalize_table(rows[0] if rows else [], rows[1:] if rows else [], source=sheet.title)
        workbook.close()
        return {**info, "kind": "workbook", "sheets": sheets}
    if fmt == "pptx":
        from pptx import Presentation
        presentation = Presentation(str(target))
        slides = []
        for slide in presentation.slides:
            slides.append(_clean_text("\n".join(shape.text for shape in slide.shapes if hasattr(shape, "text"))))
        return {**info, "kind": "slides", "slides": slides}
    sample = raw[:256]
    hex_lines = []
    for offset in range(0, len(sample), 16):
        chunk = sample[offset:offset + 16]
        printable = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        hex_lines.append(f"{offset:08X}  {chunk.hex(' ').ljust(47)}  |{printable}|")
    strings = re.findall(rb"[ -~]{6,}", raw[:min(len(raw), 64 * 1024)])
    return {**info, "kind": "binary", "sha256": hashlib.sha256(raw).hexdigest(),
            "header_hex": raw[:16].hex(), "hex_preview": "\n".join(hex_lines),
            "strings_preview": [item.decode("ascii")[:160] for item in strings[:20]],
            "preview_bytes": len(sample),
            "warning": ("Godot 二进制场景通常由引擎生成；请编辑源 .tscn 或使用 Godot 导入/导出。"
                        if fmt == "godot_binary_scene" else "二进制预览只供检查，不能据此还原完整文件。")}


def _safe_output(root: str | os.PathLike[str], path: str) -> Path:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise DataFormatError("项目目录不存在")
    candidate = Path(path)
    if candidate.is_absolute():
        raise DataFormatError("输出路径必须是项目内相对路径")
    target = (base / candidate).resolve()
    if target != base and base not in target.parents:
        raise DataFormatError("输出路径越界")
    # 与 code_root 无关的地板（凭据存储 / NT 设备命名空间），无 override。
    # 延迟导入避免与 tools 的循环依赖（tools 导入本模块）。
    # F2 修复（审计 #10/#11）：导入失败不再静默 fail-open——改为 fail-closed
    # （拒绝写入）+ 告警，杜绝「护栏不可用却悄悄放行凭据写」的敞口。
    try:
        from tools import _hard_floor_reason as _floor  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "凭据地板不可用（延迟导入 tools._hard_floor_reason 失败），"
            "为防绕过拒绝写入：%s", exc, exc_info=True)
        raise DataFormatError("安全护栏（凭据地板）不可用，拒绝写入。") from exc
    reason = _floor(str(target))
    if reason:
        raise DataFormatError(reason)
    return target


def _yaml_dump(value: Any) -> str:
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    except ImportError:
        # A conservative JSON-compatible YAML subset keeps generation useful
        # without making PyYAML a mandatory dependency.
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _xml_value(parent: ET.Element, value: Any, key: str = "item") -> None:
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            child = ET.SubElement(parent, re.sub(r"[^A-Za-z0-9_.-]", "_", str(child_key)) or "item")
            _xml_value(child, child_value, str(child_key))
    elif isinstance(value, list):
        for item in value:
            child = ET.SubElement(parent, "item")
            _xml_value(child, item, "item")
    else:
        parent.text = "" if value is None else str(value)


def write_data_file(root: str | os.PathLike[str], spec: Mapping[str, Any]) -> dict[str, Any]:
    """Generate JSON/JSONL/CSV/TSV/YAML/TOML/XML/text data and validate it."""
    path = str(spec.get("path") or "").strip()
    if not path:
        raise DataFormatError("path 不能为空")
    target = _safe_output(root, path)
    if target.exists() and not bool(spec.get("overwrite")):
        raise DataFormatError("目标文件已存在；如确需覆盖请显式传 overwrite: true")
    fmt = str(spec.get("format") or target.suffix.lstrip(".") or "json").lower()
    fmt = {"yml": "yaml", "ndjson": "jsonl", "text": "txt"}.get(fmt, fmt)
    data = spec.get("data", spec.get("value"))
    if data is None and ("rows" in spec or "headers" in spec or "records" in spec):
        data = spec.get("records", spec.get("rows", []))
    if fmt in {"binary", "bin", "scn"}:
        if target.suffix.lower() not in {".bin", ".scn"}:
            raise DataFormatError("二进制生成仅支持 .bin 或 .scn 路径")
        base64_value = spec.get("base64")
        hex_value = spec.get("hex")
        if bool(base64_value) == bool(hex_value):
            raise DataFormatError("二进制生成必须且只能提供 base64 或 hex 字节内容")
        try:
            encoded = (base64.b64decode(str(base64_value), validate=True) if base64_value
                       else bytes.fromhex(str(hex_value)))
        except (ValueError, binascii.Error) as exc:
            raise DataFormatError(f"二进制内容编码无效：{exc}") from exc
        if target.suffix.lower() == ".scn" and not encoded.startswith((b"RSRC", b"RSCC")):
            raise DataFormatError(".scn 缺少 Godot 二进制资源头；请生成 .tscn 源文件或提供真实 Godot 导出字节")
        fmt = "godot_binary_scene" if target.suffix.lower() == ".scn" else "binary"
        output = None
    elif fmt in {"csv", "tsv"}:
        table = normalize_table(spec.get("headers") or [], data if isinstance(data, list) else [], source=path)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter="\t" if fmt == "tsv" else ",", lineterminator="\n")
        writer.writerow(table["headers"])
        writer.writerows(table["rows"])
        output = stream.getvalue()
    elif fmt == "jsonl":
        records = data if isinstance(data, list) else [data]
        output = "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in records[:MAX_ROWS]) + "\n"
    elif fmt == "json":
        output = json.dumps(data if data is not None else {}, ensure_ascii=False, indent=2, default=str) + "\n"
    elif fmt == "yaml":
        output = _yaml_dump(data if data is not None else {})
    elif fmt == "toml":
        if not isinstance(data, Mapping):
            raise DataFormatError("TOML 顶层必须是对象")
        # TOML writer without an additional dependency: nested objects become tables.
        lines: list[str] = []
        def emit(obj: Mapping[str, Any], prefix: str = ""):
            scalars = [(str(k), v) for k, v in obj.items() if not isinstance(v, Mapping)]
            nested = [(str(k), v) for k, v in obj.items() if isinstance(v, Mapping)]
            for key, value in scalars:
                safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
                lines.append(f"{safe} = {json.dumps(value, ensure_ascii=False, default=str)}")
            for key, value in nested:
                section = f"{prefix}.{key}" if prefix else key
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append(f"[{section}]")
                emit(value, section)
        emit(data)
        output = "\n".join(lines) + "\n"
    elif fmt == "xml":
        root_name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(spec.get("root") or "data")) or "data"
        element = ET.Element(root_name)
        _xml_value(element, data if data is not None else {})
        output = ET.tostring(element, encoding="unicode") + "\n"
    elif fmt in {"txt", "md", "markdown"}:
        output = str(data if data is not None else spec.get("text") or "")
        if not output.endswith("\n"):
            output += "\n"
    else:
        raise DataFormatError("不支持生成格式：" + fmt)
    if output is not None:
        encoded = output.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise DataFormatError("生成内容超过 64 MB 限制")
    if fmt in {"binary", "godot_binary_scene"} and len(encoded) > MAX_READ_BYTES:
        raise DataFormatError("二进制文件超过 32 MB 回读校验上限")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # 保留目标扩展名，回读时仍能走同一格式识别逻辑；先校验临时文件，
        # 只有校验成功才替换目标，避免留下半成品或不可解析文件。
        suffix = target.suffix or ("." + fmt)
        handle = tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".docmind-data-", suffix=suffix, delete=False
        )
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.close()
        validation = detect_file(temporary)
        if fmt in {"binary", "godot_binary_scene"}:
            if validation["format"] != fmt or temporary.read_bytes() != encoded:
                raise DataFormatError("二进制文件回读校验失败")
            validation["sha256"] = hashlib.sha256(encoded).hexdigest()
            if fmt == "godot_binary_scene":
                validation["warning"] = "仅校验资源头与字节一致性；场景可加载性需由 Godot 引擎验证"
        parsed = None
        if fmt in {"csv", "tsv", "json", "jsonl", "yaml", "toml", "xml"}:
            parsed = read_file(temporary)
            validation["parsed_kind"] = parsed.get("kind")
            validation["row_count"] = parsed.get("row_count")
            validation["errors"] = parsed.get("errors", [])
            if validation["errors"]:
                raise DataFormatError(
                    "生成文件回读校验失败：" + str(validation["errors"][0].get("error", "格式错误"))
                )
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {"ok": True, "path": str(target), "format": fmt, "bytes": len(encoded), "validation": validation}


__all__ = ["DataFormatError", "detect_file", "read_file", "normalize_table", "write_data_file"]
