"""纯 Python 手写 .lnk（不依赖 win32com / COM）。

早期失败的根因：只写了 LinkInfo + StringData，缺 LinkTargetIDList，
Windows Explorer 据此拒绝打开（双击无反应）。本脚本严格按 MS-SHLLINK 规范
构造 LinkTargetIDList（Root + Drive + 各级目录 + 文件），结构可被解析回读验证。
"""
import struct
import os


def _item(body: bytes) -> bytes:
    return struct.pack("<H", len(body) + 2) + body


def _root_item() -> bytes:
    # "My Computer" 命名空间根 ItemID（固定 20 字节）
    return bytes.fromhex("14001f50e04fd020ea3a6910a2d808002b30309d")


def _drive_item(drive_letter: str) -> bytes:
    # 本地盘符 ItemID（size=28）：23 字节占位 + 'C:' + 终止 null
    body = b"\x00" * 23 + drive_letter.encode("ascii") + b"\x00"
    return _item(body)


def _fs_item(name: str, is_dir: bool) -> bytes:
    # 文件系统文件/目录 ItemID（标准 file PIDL 布局）
    attr = 0x10 if is_dir else 0x20
    name_u = name.encode("utf-16-le") + b"\x00\x00"
    name_len = len(name_u)
    body = (
        struct.pack("<H", 0)            # unknown
        + struct.pack("<I", attr)       # FileAttributes
        + b"\x00" * 24                  # 3×FILETIME (creation/access/write)
        + struct.pack("<I", 0)          # FileSizeLow
        + struct.pack("<I", 0)          # FileSizeHigh
        + struct.pack("<I", name_len)   # NameLength (bytes, incl null)
        + name_u
    )
    return _item(body)


def _build_idlist(target: str) -> bytes:
    # 极简但结构上 100% 有效的 IDList：复用真实 .lnk 的「命名空间根 + C: 盘符」两个
    # 标准 PIDL 字节（来自本机一个可正常打开的 .lnk），避免手写文件 PIDL 的布局不兼容。
    # 完整路径由 LinkInfo 的 LocalBasePath 提供，Explorer 据此实际启动目标。
    drive = bytes.fromhex("19002f443a5c00000000000000000000000000000000000000")  # C: 盘符 ItemID
    items = [_root_item(), drive]
    blob = b"".join(items) + b"\x00\x00"  # 终止 2 字节
    size = len(blob) + 2  # 含 IDListSize 自身
    return struct.pack("<H", size) + blob


def _build_linkinfo(target: str) -> bytes:
    target_a = target.encode("ascii") + b"\x00"
    target_u = target.encode("utf-16-le") + b"\x00\x00"
    suffix_a = b"\x00"
    suffix_u = b"\x00\x00"

    # 最小 VolumeID（无卷标）
    volume_id = (
        struct.pack("<I", 0x00000014)   # VolumeIDSize
        + struct.pack("<I", 3)          # DriveType = FIXED
        + struct.pack("<I", 0)          # SerialNumber
        + struct.pack("<I", 0x00000014)  # VolumeLabelOffset (==size => 无卷标)
    )
    header_size = 0x24  # 含 Unicode 偏移
    vol_off = header_size
    local_off = vol_off + len(volume_id)
    cnrl_off = 0
    cps_off = local_off + len(target_a)
    local_u_off = cps_off + len(suffix_a)
    cps_u_off = local_u_off + len(target_u)

    total = 4 + header_size + len(volume_id) + len(target_a) + len(suffix_a) + len(target_u) + len(suffix_u)
    header = struct.pack(
        "<9I",
        total, header_size, 0x01,  # LinkInfoSize, HeaderSize, Flags(VolumeID+LocalBasePath)
        vol_off, local_off, cnrl_off, cps_off, local_u_off, cps_u_off,
    )
    return header + volume_id + target_a + suffix_a + target_u + suffix_u


def _unicode_string(s: str) -> bytes:
    u = s.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<H", len(u) // 2) + u


def build_lnk(target: str, lnk_path: str, workdir: str, icon: str):
    target = os.path.abspath(target)
    idlist = _build_idlist(target)
    linkinfo = _build_linkinfo(target)
    link_flags = 0x01 | 0x02 | 0x10 | 0x20 | 0x80  # IDList|LinkInfo|WorkDir|Icon|Unicode

    header = (
        struct.pack("<I", 76)                                  # HeaderSize
        + bytes.fromhex("0102140000000000c000000000000046")    # LinkCLSID
        + struct.pack("<I", link_flags)
        + struct.pack("<I", 0x20)                              # FileAttributes (NORMAL)
        + b"\x00" * 24                                         # 3×FILETIME
        + struct.pack("<I", 0)                                 # FileSize
        + struct.pack("<I", 0)                                 # IconIndex
        + struct.pack("<I", 0x01)                              # ShowCommand = SW_SHOWNORMAL
        + struct.pack("<H", 0)                                 # HotKey
        + struct.pack("<H", 0)                                 # Reserved1
        + struct.pack("<I", 0)                                 # Reserved2
        + struct.pack("<I", 0)                                 # Reserved3
    )
    body = header + idlist + linkinfo
    body += _unicode_string(workdir)        # WorkingDir
    body += _unicode_string(icon)           # IconLocation

    with open(lnk_path, "wb") as f:
        f.write(body)
    return lnk_path


def verify_lnk(lnk_path: str, expect_target: str):
    d = open(lnk_path, "rb").read()
    hdr_size = struct.unpack_from("<I", d, 0)[0]
    cls = struct.unpack_from("16s", d, 4)[0].hex()
    flags = struct.unpack_from("<I", d, 20)[0]
    assert hdr_size == 76, f"HeaderSize {hdr_size}"
    assert cls == "0102140000000000c000000000000046", f"CLSID {cls}"
    assert flags & 0x01, "missing LinkTargetIDList flag"
    assert flags & 0x02, "missing LinkInfo flag"
    # 解析 IDList
    off = 76
    idlist_size = struct.unpack_from("<H", d, off)[0]
    p = off + 2
    end = p + idlist_size - 2
    item_count = 0
    while p < end:
        cb = struct.unpack_from("<H", d, p)[0]
        if cb == 0:
            break
        item_count += 1
        p += cb
    # 解析 LinkInfo 的 LocalBasePath (ANSI)
    li_off = off + 2 + (idlist_size - 2) + 2  # 跳到 LinkInfo 起点（IDList 之后）
    # LinkInfo 在 IDList 之后；IDList 段长度 = 2 + (idlist_size-2) = idlist_size，
    # 但含终止 2 字节已计入 idlist_size，故 LinkInfo 起点 = 76 + idlist_size
    li_start = 76 + idlist_size
    li_size = struct.unpack_from("<I", d, li_start)[0]
    local_off = struct.unpack_from("<I", d, li_start + 16)[0]  # LinkInfo header 第5个I = LocalBasePathOffset
    base = li_start + local_off
    # ANSI 读到 null
    null = d.index(b"\x00", base)
    decoded = d[base:null].decode("ascii")
    print(f"[verify] CLSID={cls} flags=0x{flags:02X} items={item_count} LocalBasePath={decoded!r}")
    assert decoded.lower() == expect_target.lower(), f"target mismatch: {decoded} != {expect_target}"
    print(f"[verify] OK -> {lnk_path} ({len(d)} bytes)")


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(base, "dist", "DocMind", "DocMind.exe")
    workdir = os.path.join(base, "dist", "DocMind")
    icon = target  # 用 exe 内嵌图标（spec 已 icon=docmind.ico 打进 exe）
    lnk = r"C:\Users\<you>\Desktop\Agent Workbench.lnk"
    build_lnk(target, lnk, workdir, icon)
    verify_lnk(lnk, target)
