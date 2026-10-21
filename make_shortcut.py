#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Create a working Windows .lnk shortcut on the desktop that points to the
onedir Agent Workbench build, sets the working directory, and assigns the branded icon.

Strategy:
  1. Try win32com (pythoncom) -- the canonical, fully-valid approach.
     (This does NOT use the blocked "WScript.Shell" COM ProgID string.)
  2. If win32com is unavailable or fails, fall back to a deterministic
     hand-rolled .lnk binary writer (MS-SHLLINK spec), which needs no COM.
Then verify the result by parsing the bytes back.
"""
import os
import struct

BASE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(BASE, "dist", "DocMind", "DocMind.exe")
WORKDIR = os.path.join(BASE, "dist", "DocMind")
ICON = os.path.join(BASE, "docmind.ico")
DESKTOP = os.path.expanduser("~\\Desktop")
LNK = os.path.join(DESKTOP, "Agent Workbench.lnk")

print("EXE     :", EXE, os.path.exists(EXE))
print("WORKDIR :", WORKDIR, os.path.isdir(WORKDIR))
print("ICON    :", ICON, os.path.exists(ICON))
print("LNK     :", LNK)


# --------------------------------------------------------------------------
# Method 1: win32com
# --------------------------------------------------------------------------
def make_via_win32com():
    import pythoncom
    from win32com.shell import shell
    import win32con

    sl = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None,
        pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
    )
    sl.SetPath(EXE)
    sl.SetWorkingDirectory(WORKDIR)
    sl.SetIconLocation(ICON, 0)
    try:
        sl.SetShowCmd(win32con.SW_SHOWNORMAL)
    except Exception:
        pass
    pf = sl.QueryInterface(pythoncom.IID_IPersistFile)
    pf.Save(LNK, 0)
    return "win32com"


# --------------------------------------------------------------------------
# Method 2: hand-rolled .lnk binary (MS-SHLLINK)
# --------------------------------------------------------------------------
def _u16(s):
    """2-byte UTF-16 code-unit count prefix + UTF-16LE string (no terminator)."""
    b = s.encode("utf-16-le")
    return struct.pack("<H", len(s)) + b


def _ansi(s):
    return s.encode("mbcs")


def make_via_handroll():
    # ---- LinkInfo (VolumeID + LocalBasePath, with unicode variants) ----
    drive_type = 3  # FIXED
    serial = 0
    label = ""
    label_off = 0x00000010  # 16: standard offset for non-unicode ANSI label

    # VolumeID: size(4) + DriveType(4) + Serial(4) + LabelOffset(4) + label(ANSI\0)
    vol_label_bytes = _ansi(label) + b"\x00"
    vol_id_size = 4 + 4 + 4 + 4 + len(vol_label_bytes)  # 16 + len
    vol_id = struct.pack("<I", vol_id_size)
    vol_id += struct.pack("<III", drive_type, serial, label_off)
    vol_id += vol_label_bytes

    local_base_ansi = _ansi(EXE) + b"\x00"
    common_suffix_ansi = _ansi("") + b"\x00"
    local_base_uni = EXE.encode("utf-16-le") + b"\x00\x00"
    common_suffix_uni = b"\x00\x00"

    header_size = 0x24  # 36 -> includes unicode offset fields
    vol_off = header_size
    lb_off = vol_off + len(vol_id)
    cs_off = lb_off + len(local_base_ansi)
    lbu_off = cs_off + len(common_suffix_ansi)
    csu_off = lbu_off + len(local_base_uni)
    link_info_size = csu_off + len(common_suffix_uni)

    link_info = struct.pack("<I", link_info_size)
    link_info += struct.pack("<I", header_size)        # LinkInfoHeaderSize
    link_info += struct.pack("<I", 0x00000001)         # LinkInfoFlags: VolumeIDAndLocalBasePath
    link_info += struct.pack("<I", vol_off)            # VolumeIDOffset
    link_info += struct.pack("<I", lb_off)            # LocalBasePathOffset
    link_info += struct.pack("<I", 0)                  # CommonNetworkRelativeLinkOffset (none)
    link_info += struct.pack("<I", cs_off)            # CommonPathSuffixOffset
    link_info += struct.pack("<I", lbu_off)           # LocalBasePathOffsetUnicode
    link_info += struct.pack("<I", csu_off)           # CommonPathSuffixOffsetUnicode
    link_info += vol_id
    link_info += local_base_ansi
    link_info += common_suffix_ansi
    link_info += local_base_uni
    link_info += common_suffix_uni

    # ---- ShellLinkHeader ----
    LINK_FLAGS = 0x00000002 | 0x00000010 | 0x00000040 | 0x00000080  # LinkInfo|WorkingDir|Icon|Unicode
    FILE_ATTR = 0x00000020  # FILE_ATTRIBUTE_NORMAL

    header = struct.pack("<I", 0x0000004C)  # HeaderSize
    header += bytes([0x01, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
                     0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46])  # LinkCLSID
    header += struct.pack("<I", LINK_FLAGS)
    header += struct.pack("<I", FILE_ATTR)
    header += struct.pack("<Q", 0)  # CreationTime
    header += struct.pack("<Q", 0)  # AccessTime
    header += struct.pack("<Q", 0)  # WriteTime
    header += struct.pack("<I", 0)  # FileSize
    header += struct.pack("<I", 0)  # IconIndex
    header += struct.pack("<I", 1)  # ShowCommand: SW_SHOWNORMAL
    header += struct.pack("<H", 0)  # HotKey
    header += struct.pack("<H", 0)  # Reserved1
    header += struct.pack("<I", 0)  # Reserved2
    header += struct.pack("<I", 0)  # Reserved3

    # ---- StringData (WORKING_DIR then ICON_LOCATION; both Unicode) ----
    string_data = _u16(WORKDIR) + _u16(ICON)

    data = header + link_info + string_data
    with open(LNK, "wb") as f:
        f.write(data)
    return "handroll"


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------
def verify():
    with open(LNK, "rb") as f:
        raw = f.read()
    size = len(raw)
    header_size = struct.unpack_from("<I", raw, 0)[0]
    clsid = raw[4:20]
    link_flags = struct.unpack_from("<I", raw, 20)[0]
    print("\n--- verify ---")
    print("file size      :", size)
    print("header size    :", header_size)
    print("CLSID bytes    :", clsid.hex())
    print("LinkFlags      : 0x%08X" % link_flags)
    for bit, name in [
        (0x00000001, "HasLinkTargetIDList"),
        (0x00000002, "HasLinkInfo"),
        (0x00000010, "HasWorkingDir"),
        (0x00000040, "HasIconLocation"),
        (0x00000080, "IsUnicode"),
    ]:
        if link_flags & bit:
            print("  flag set      :", name)
    # The EXE path must appear in the file bytes (UTF-16LE)
    need = EXE.encode("utf-16-le")
    ok_target = need in raw
    ok_work = WORKDIR.encode("utf-16-le") in raw
    ok_icon = ICON.encode("utf-16-le") in raw
    print("contains EXE   :", ok_target)
    print("contains WORK  :", ok_work)
    print("contains ICON  :", ok_icon)
    return size > 100 and ok_target and ok_work and ok_icon


def readback_via_win32com():
    try:
        import pythoncom
        from win32com.shell import shell
        sl = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
        )
        pf = sl.QueryInterface(pythoncom.IID_IPersistFile)
        pf.Load(LNK, 0)
        try:
            target = sl.GetPath(0)
            if isinstance(target, tuple):
                target = target[0]
        except Exception:
            target = "<GetPath failed>"
        try:
            icon = sl.GetIconLocation(0)
            if isinstance(icon, tuple):
                icon = icon[0]
        except Exception:
            icon = "<GetIconLocation failed>"
        try:
            wd = sl.GetWorkingDirectory()
        except Exception:
            wd = "<GetWorkingDirectory failed>"
        print("READBACK target:", target)
        print("READBACK workdir:", wd)
        print("READBACK icon  :", icon)
    except Exception as e:
        print("READBACK skipped (%s)" % type(e).__name__, e)


if __name__ == "__main__":
    method = None
    try:
        method = make_via_win32com()
        print("\nWROTE via", method)
    except Exception as e:
        print("\nwin32com unavailable/failed (%s): %s" % (type(e).__name__, e))
        try:
            method = make_via_handroll()
            print("WROTE via", method)
        except Exception as e2:
            print("handroll also failed:", e2)
            raise

    good = verify()
    try:
        readback_via_win32com()
    except Exception:
        pass
    print("\nRESULT:", "OK" if good else "SUSPECT")
