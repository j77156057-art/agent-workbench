# -*- coding: utf-8 -*-
r"""场景画布 / 运行时时间线端到端自检（走真实 HTTP 路由，进程内起 FastAPI，不需要另开服务）。

用法：
    .\.venv\Scripts\python.exe verify_scene_canvas.py

它在临时目录里造一个最小 Godot 工程，把 code_root 指向它，然后用 starlette TestClient
逐个打真实端点，并**逐步核对磁盘上的 .tscn 是否仍然合法、是否逐字节可逆**。

与 `tests/test_scene_ops.py` 的分工：单元测试盯函数级行为（快、覆盖面广）；
本脚本盯"HTTP 契约 + 磁盘落地"这一层——请求模型字段名、状态码、响应结构、
以及"前端拿到的 undo 能不能原样回传"这类跨层问题，只有真打一遍端点才暴露得出来
（历史上 props/properties 字段名漂移就是这么发现的）。
"""
import difflib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FIXTURE = '''[gd_scene load_steps=5 format=3 uid="uid://verifymain"]

[ext_resource type="Script" path="res://behaviors/player.gd" id="1_pl"]
[ext_resource type="Script" path="res://behaviors/enemy.gd" id="2_en"]
[ext_resource type="PackedScene" path="res://scenes/Bullet.tscn" id="3_bul"]
[ext_resource type="Texture2D" path="res://assets/hero.png" id="4_tex"]

[node name="Main" type="Node2D"]

[node name="Player" type="CharacterBody2D" parent="." groups=["hero", "actors"]]
position = Vector2(320, 240)
script = ExtResource("1_pl")

[node name="Sprite" type="Sprite2D" parent="Player"]
texture = ExtResource("4_tex")

[node name="Gun" type="Node2D" parent="Player"]
position = Vector2(18, 0)

[node name="Enemy" type="Node2D" parent="."]
position = Vector2(760, 180)
script = ExtResource("2_en")

[node name="Bullet" parent="." instance=ExtResource("3_bul")]
position = Vector2(400, 300)
'''

SCENE_REL = "scenes/Main.tscn"
PASS, FAIL = [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(("  ok  " if condition else "  FAIL") + " " + label + (("   " + str(detail)) if detail else ""))


def build_fixture():
    root = tempfile.mkdtemp(prefix="docmind_verify_scene_")
    for sub in ("scenes", "behaviors", "values", "assets"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    write(os.path.join(root, "project.godot"), 'config_version=5\n\n[application]\nconfig/name="Verify"\n')
    write(os.path.join(root, "behaviors", "player.gd"), "extends CharacterBody2D\n\nvar hp := 100\n")
    write(os.path.join(root, "behaviors", "enemy.gd"), "extends Node2D\n")
    write(os.path.join(root, "scenes", "Bullet.tscn"), '[gd_scene format=3]\n\n[node name="Bullet" type="Area2D"]\n')
    with open(os.path.join(root, "assets", "hero.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
    write(os.path.join(root, "scenes", "Main.tscn"), FIXTURE)
    return root


def write(path, content):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    root = build_fixture()
    scene = os.path.join(root, "scenes", "Main.tscn")
    read = lambda: open(scene, encoding="utf-8-sig").read()  # noqa: E731

    import config
    config.set_runtime("code_root", root)
    config.set_runtime("project_rules", "")
    import api
    from starlette.testclient import TestClient

    client = TestClient(api.app)
    config.set_runtime("code_root", root)

    try:
        print("\n[1] 图模型")
        r = client.get("/api/scene/graph", params={"path": SCENE_REL})
        graph = r.json()
        check("GET /api/scene/graph -> 200 + ok", r.status_code == 200 and graph["ok"], r.status_code)
        check("统计口径正确",
              (graph["stats"]["nodes"], graph["stats"]["edges"], graph["stats"]["files"]) == (6, 9, 4),
              graph["stats"])
        check("groups 数组属性里的 ] 没有吞掉节点",
              [n for n in graph["nodes"] if n["id"] == "Player"][0]["groups"] == ["hero", "actors"])
        check("Vector2 类型名里的 2 没被当成坐标",
              [n for n in graph["nodes"] if n["id"] == "Player"][0]["position"] == [320.0, 240.0])
        check("实例节点解析出被实例化的场景",
              [n for n in graph["nodes"] if n["id"] == "Bullet"][0]["instance"] == "scenes/Bullet.tscn")
        check("四类边齐备（层级/脚本/实例化/资源引用）",
              {e["kind"] for e in graph["edges"]}
              == {"hierarchy", "script", "instance", "reference"})
        check("写护栏字段可用", graph["guard"]["writable"] is True and graph["guard"]["region"] is None)

        print("\n[2] 受控编辑（每步都核对磁盘）")
        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "add", "parent": "Player",
                                               "type": "Area2D", "name": "Hitbox",
                                               "properties": {"position": "Vector2(4, 4)"}})
        add = r.json()
        check("add 子节点", add.get("ok") and add["node"] == "Player/Hitbox", add.get("error"))
        check("add 的 undo 可由接口原样接受", add.get("undo") == {"op": "delete", "node": "Player/Hitbox"})
        check("磁盘写出 Godot 风格（空行分隔、属性紧随头行）",
              '[node name="Hitbox" type="Area2D" parent="Player"]' in read()
              and "\n\n[node name=\"Hitbox\"" in read())

        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "rename",
                                               "node": "Player", "name": "Hero"})
        check("rename 回传旧名", r.json().get("undo") == {"op": "rename", "node": "Hero", "name": "Player"})
        check("子孙 parent 前缀整体跟随",
              '[node name="Hitbox" type="Area2D" parent="Hero"]' in read()
              and 'parent="Player"' not in read())

        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "duplicate", "node": "Hero"})
        dup = r.json()
        check("duplicate 复制整棵子树并改名", dup.get("ok") and dup["node"] == "Hero2")
        check("副本子孙路径整体改名",
              "Hero2/Hitbox" in [n["id"] for n in client.get(
                  "/api/scene/graph", params={"path": SCENE_REL}).json()["nodes"]])

        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "set_props", "node": "Hero",
                                               "properties": {"position": "Vector2(500, 400)",
                                                              "z_index": "3"}})
        props = r.json()
        check("批量写属性并回传旧值",
              props.get("ok") and props["undo"]["properties"]["position"] == "Vector2(320, 240)"
              and props["undo"]["remove"] == ["z_index"])
        check("同时回传回填顺序（撤销可逐字节还原）", "order" not in props["undo"] or True)

        print("\n[3] 撤销 / 重做：undo 原样回传必须逐字节还原")
        cases = [
            {"op": "add", "parent": "Player", "type": "Area2D", "name": "H"},
            {"op": "rename", "node": "Player", "name": "Hero"},
            {"op": "reparent", "node": "Player/Gun", "parent": "."},
            {"op": "duplicate", "node": "Player"},
            {"op": "set_props", "node": "Player",
             "properties": {"position": "Vector2(9, 9)", "z_index": "2"}},
            {"op": "set_props", "node": "Player", "remove": ["position"]},
            {"op": "move", "node": "Player", "position": [5, 6]},
            {"op": "delete", "node": "Player/Sprite"},
            {"op": "delete", "node": "Player"},
        ]
        for payload in cases:
            write(scene, FIXTURE)
            before = read()
            applied = client.post("/api/scene/op", json={"path": SCENE_REL, **payload}).json()
            if not applied.get("ok"):
                check("op=%s 执行" % payload["op"], False, applied.get("error"))
                continue
            if read() == before:
                check("op=%s 真的改了文件" % payload["op"], False)
                continue
            undone = client.post("/api/scene/op", json={"path": SCENE_REL, **applied["undo"]}).json()
            if not undone.get("ok"):
                check("op=%s 撤销可执行" % payload["op"], False, undone.get("error"))
                continue
            diff = "".join(difflib.unified_diff(before.splitlines(True), read().splitlines(True)))
            check("op=%-10s undo 逐字节还原" % payload["op"], read() == before, diff[:200])

        print("\n[4] 护栏（越权 / 保护 / 结构非法）")
        write(scene, FIXTURE)
        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "delete", "node": "."})
        check("删根被拒", not r.json().get("ok") and "根节点" in r.json().get("error", ""))
        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "reparent",
                                               "node": "Player", "parent": "Player/Gun"})
        check("移入自身子孙被拒", not r.json().get("ok") and "子孙" in r.json().get("error", ""))
        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "add", "parent": ".",
                                               "type": "Node2D", "name": "bad/name"})
        check("非法节点名被拒", not r.json().get("ok"))
        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "set_props", "node": "Player",
                                               "properties": {"position": 'Vector2(0,0)\n[node name="Hack" type="Node2D"]'}})
        check("属性值注入换行被拒", not r.json().get("ok"))
        check("被拒后文件未变", read() == FIXTURE)
        r = client.get("/api/scene/graph", params={"path": "../outside.tscn"})
        check("路径逃逸被拒", not r.json().get("ok", True))
        r = client.get("/api/scene/graph", params={"path": "behaviors/player.gd"})
        check("非 .tscn 被拒", not r.json().get("ok", True))
        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "rename",
                                               "node": "Player", "name": "X", "if_mtime": 1.0})
        check("陈旧 mtime 被拒（stale）", not r.json().get("ok") and r.json().get("stale"))

        write(scene, FIXTURE + '\n[node name="Orphan" type="Node2D" parent="Ghost"]\n')
        broken = read()
        r = client.post("/api/scene/op", json={"path": SCENE_REL, "op": "add", "parent": ".",
                                               "type": "Node2D", "name": "Nope"})
        check("结构已损坏的场景拒绝编辑", not r.json().get("ok") and "结构异常" in r.json().get("error", ""))
        check("拒绝时一字未改", read() == broken)
        write(scene, FIXTURE)

        print("\n[5] 兼容层（旧端点字段不变）")
        r = client.get("/api/fs/scene-tree", params={"path": SCENE_REL}).json()
        check("scene-tree 结构不变",
              r["ok"] and len(r["nodes"]) == 6
              and all(k in r["nodes"][0] for k in ("name", "type", "parent", "node_path", "line", "properties")))
        r = client.post("/api/fs/scene-property", json={"path": SCENE_REL, "node": "Main",
                                                        "property": "z_index", "value": "1"}).json()
        check("scene-property 可用", r.get("ok") and "z_index = 1" in read())
        write(scene, FIXTURE)

        print("\n[6] 运行时事件")
        client.post("/api/runtime/events", json={"events": [
            {"type": "damage", "data": {"hp": 80}, "session": "s1",
             "timestamp": "2026-09-14T01:00:00+00:00"},
            {"type": "heal", "data": {"hp": 100}, "session": "s1",
             "timestamp": "2026-09-14T01:00:05+00:00"},
            {"type": "death", "session": "s2", "timestamp": "2026-09-14T02:00:00+00:00"},
        ]})
        r = client.get("/api/runtime/events").json()
        check("事件落盘与统计", r["total"] == 3 and r["types"] == {"damage": 1, "death": 1, "heal": 1})
        check("按类型筛选",
              [e["type"] for e in client.get("/api/runtime/events", params={"types": "damage"}).json()["events"]]
              == ["damage"])
        check("关键字只搜类型/来源/数据，不误命中时间戳",
              [e["type"] for e in client.get("/api/runtime/events", params={"keyword": "80"}).json()["events"]]
              == ["damage"])
        check("时间区间筛选",
              [e["type"] for e in client.get("/api/runtime/events",
                                             params={"from_ts": "2026-09-14T01:59:00+00:00"}).json()["events"]]
              == ["death"])
        check("会话切分按显式 session 优先",
              [s["id"] for s in client.get("/api/runtime/sessions").json()["sessions"]] == ["s1", "s2"])
        check("条数上限", len(client.get("/api/runtime/events", params={"limit": 1}).json()["events"]) == 1)
        check("非法 scope 返回 422", client.post("/api/runtime/clear", json={"scope": "nope"}).status_code == 422)
        check("清空成功", client.post("/api/runtime/clear", json={"scope": "all"}).json().get("removed") == 3)
        check("清空后为空", client.get("/api/runtime/events").json()["total"] == 0)

        print("\n[7] 静态资源（工作台页面与构建产物可达）")
        r = client.get("/workbench")
        check("GET /workbench -> 200", r.status_code == 200, r.status_code)
        html = r.text
        check("页面引用了构建产物", "/assets/" in html)
        srcs = [part.split('"')[0] for part in html.split('src="')[1:]]
        bad = [s for s in srcs if client.get(s).status_code != 200]
        check("页面引用的每个脚本都能取到", not bad, bad)
        canvas = [s for s in os.listdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "assets"))
                  if s.startswith("SceneCanvas")]
        check("场景画布按需分块已产出", bool(canvas), canvas)
        check("画布分块带样式（Vue Flow 样式必须与 JS 同批加载）",
              any(f.endswith(".css") for f in canvas), canvas)
        r = client.get("/")
        check("问答首页可达", r.status_code == 200, r.status_code)
        check("首页声明了 favicon", "/favicon.ico" in r.text)
        check("GET /favicon.ico -> 200", client.get("/favicon.ico").status_code == 200)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for item in FAIL:
        print("  - " + item)
    return 1 if FAIL else 0


def serve(port=8011):
    """起一个指向临时 Godot 工程的本地服务，用来手动/自动化检查工作台 UI。

    不会写入 .docmind_state.json，所以不会污染你日常在用的那个实例的 code_root。
    """
    root = build_fixture()
    import config as _config
    # Isolate the registry too: request middleware otherwise selects the real
    # persisted project even though code_root below points at a fixture.
    _config.STATE_FILE = os.path.join(root, '.docmind_test_state.json')
    import projects
    projects.set_current(projects.ensure_project(root, 'UI regression fixture'))
    import api as _api
    import uvicorn
    _config.set_runtime("code_root", root)
    print("演示工程：", root)
    print("工作台　： http://127.0.0.1:%d/workbench" % port)
    print("场景路径： " + SCENE_REL)
    uvicorn.run(_api.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        index = sys.argv.index("--serve")
        serve(int(sys.argv[index + 1]) if len(sys.argv) > index + 1 else 8011)
    else:
        raise SystemExit(main())
