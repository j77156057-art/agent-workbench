# -*- coding: utf-8 -*-
"""P1 关系图构建器单元测试（workbench_fs.build_relation_graph）。

覆盖：GDScript extends 项目内/引擎两种去向、res:// 路径继承、Python bases
（项目内直连/外部聚合/object 隐式省略/泛型逗号）、.tscn 挂载边与去重、
匿名脚本/场景节点形态、分区归属、空项目。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workbench_fs as wb  # noqa: E402


def _write(root, rel, content):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path.replace("\\", "/")


def _node(graph, nid):
    for n in graph["nodes"]:
        if n["id"] == nid:
            return n
    return None


def _edges(graph, kind):
    return [(e["source"], e["target"]) for e in graph["edges"] if e["kind"] == kind]


class RelationGraphTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name.replace("\\", "/")

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_root(self):
        g = wb.build_relation_graph(self.root)
        self.assertTrue(g["ok"])
        self.assertEqual(g["nodes"], [])
        self.assertEqual(g["edges"], [])
        self.assertEqual(g["stats"]["edges"], 0)

    def test_gd_inherits_user_class_and_engine(self):
        _write(self.root, "values/base.gd", "class_name BaseThing\n\nvar x: int = 1\n")
        _write(self.root, "behaviors/child.gd", "extends BaseThing\n\nfunc f():\n\tpass\n")
        _write(self.root, "behaviors/enemy.gd", "extends CharacterBody3D\n\nfunc run():\n\tpass\n")
        g = wb.build_relation_graph(self.root)

        # 节点形态：命名类 / 匿名脚本 / 引擎外部节点
        base = _node(g, "gd:values/base.gd")
        child = _node(g, "gd:behaviors/child.gd")
        _node(g, "gd:behaviors/enemy.gd")
        engine = _node(g, "ext:engine:CharacterBody3D")
        self.assertEqual(base["kind"], "class")
        self.assertEqual(base["label"], "BaseThing")
        self.assertEqual(child["kind"], "script")
        self.assertEqual(child["label"], "child")
        self.assertEqual(child["sub"], "behaviors/child.gd")
        self.assertFalse(child["external"])
        self.assertTrue(engine["external"])
        self.assertEqual(engine["rel"], "")

        inh = _edges(g, "inherits")
        self.assertIn(("gd:behaviors/child.gd", "gd:values/base.gd"), inh)
        self.assertIn(("gd:behaviors/enemy.gd", "ext:engine:CharacterBody3D"), inh)

        # 默认分区生效
        self.assertEqual(child["region"], "behaviors")
        self.assertEqual(base["region"], "values")
        self.assertEqual(g["stats"]["edges_by_kind"]["inherits"], 2)

    def test_gd_no_extends_no_edge(self):
        # 隐式 RefCounted 不产生边
        _write(self.root, "values/cfg.gd", "class_name Cfg\n\nconst A: int = 1\n")
        g = wb.build_relation_graph(self.root)
        self.assertEqual(g["edges"], [])
        self.assertEqual(g["stats"]["external_nodes"], 0)

    def test_gd_res_path_extends(self):
        _write(self.root, "base.gd", "extends RefCounted\n")
        _write(self.root, "derived.gd", 'extends "res://base.gd"\n')
        g = wb.build_relation_graph(self.root)
        inh = _edges(g, "inherits")
        self.assertIn(("gd:derived.gd", "gd:base.gd"), inh)

    def test_python_bases(self):
        _write(self.root, "models.py", (
            "class Base:\n"
            "    pass\n"
            "\n"
            "class Child(Base):\n"
            "    pass\n"
            "\n"
            "class Plain(object):\n"
            "    pass\n"
            "\n"
            "class External(typing.Generic[T], abc.ABC):\n"
            "    pass\n"
        ))
        g = wb.build_relation_graph(self.root)
        child = _node(g, "py:models.py:Child")
        self.assertEqual(child["kind"], "class")
        self.assertEqual(child["line"], 4)
        inh = _edges(g, "inherits")
        self.assertIn(("py:models.py:Child", "py:models.py:Base"), inh)
        # object 隐式基类省略
        self.assertNotIn(("py:models.py:Plain", "ext:external:object"), inh)
        # 泛型参数里的逗号不会拆出 T / abc 之类噪声；Generic 与 ABC 为外部节点
        self.assertIn(
            ("py:models.py:External", "ext:external:Generic"), inh
        )
        self.assertIn(
            ("py:models.py:External", "ext:external:ABC"), inh
        )
        ext = _node(g, "ext:external:Generic")
        self.assertTrue(ext["external"])
        self.assertEqual(ext["sub"], "python")

    def test_python_generic_dict_comma(self):
        # Dict[str, int] 的内层逗号不能被切成两个基类
        parts = wb._split_top_commas("Dict[str, int], Base")
        self.assertEqual(parts, ["Dict[str, int]", "Base"])
        self.assertEqual(wb._base_simple_name("pkg.mod.Foo[T]"), "Foo")

    def test_scene_mounts(self):
        _write(self.root, "behaviors/player.gd", "extends CharacterBody3D\n")
        _write(self.root, "behaviors/enemy.gd", "extends CharacterBody3D\n")
        _write(self.root, "ui/hud.gd", "extends CanvasLayer\n")
        _write(self.root, "scenes/main.tscn", (
            '[gd_scene load_steps=4 format=3]\n'
            '\n'
            '[ext_resource type="Script" path="res://behaviors/player.gd" id="1_p"]\n'
            '[ext_resource type="Script" path="res://behaviors/enemy.gd" id="2_e"]\n'
            '[ext_resource type="PackedScene" path="res://scenes/other.tscn" id="3_s"]\n'
            '\n'
            '[node name="Main" type="Node3D"]\n'
            '\n'
            '[node name="Player" type="CharacterBody3D" parent="."]\n'
            'script = ExtResource("1_p")\n'
            '\n'
            '[node name="Player2" type="CharacterBody3D" parent="."]\n'
            'script = ExtResource("1_p")\n'
            '\n'
            '[node name="Enemy" type="CharacterBody3D" parent="."]\n'
            'script = ExtResource("2_e")\n'
            '\n'
            '[node name="HUD" type="CanvasLayer" parent="."]\n'
        ))
        g = wb.build_relation_graph(self.root)

        scene = _node(g, "scene:scenes/main.tscn")
        self.assertEqual(scene["kind"], "scene")
        self.assertEqual(scene["label"], "main")
        mounts = _edges(g, "mounts")
        # Player 挂两次（两个节点同脚本）→ 边去重
        self.assertIn(("scene:scenes/main.tscn", "gd:behaviors/player.gd"), mounts)
        self.assertIn(("scene:scenes/main.tscn", "gd:behaviors/enemy.gd"), mounts)
        self.assertEqual(len(mounts), 2)
        # PackedScene 不是脚本；HUD 节点没挂脚本 → 无关联
        self.assertNotIn(("scene:scenes/main.tscn", "gd:ui/hud.gd"), mounts)

        # 挂载边带脚本赋值行号
        for e in g["edges"]:
            if e["kind"] == "mounts" and e["target"] == "gd:behaviors/player.gd":
                self.assertGreater(e["line"], 0)
        # 场景本身无继承边噪声
        self.assertEqual(g["stats"]["edges_by_kind"].get("mounts"), 2)

    def test_scene_script_missing_target_skipped(self):
        _write(self.root, "scenes/lonely.tscn", (
            '[gd_scene load_steps=2 format=3]\n'
            '[ext_resource type="Script" path="res://ghost.gd" id="1_g"]\n'
            '[node name="Root" type="Node"]\n'
            'script = ExtResource("1_g")\n'
        ))
        g = wb.build_relation_graph(self.root)
        self.assertEqual(g["edges"], [])  # ghost.gd 不存在 → 不出边也不建外部噪声

    def test_stats_shape(self):
        _write(self.root, "a.gd", "extends Node\n")
        g = wb.build_relation_graph(self.root)
        st = g["stats"]
        self.assertEqual(st["files"], 1)
        self.assertEqual(st["nodes"], 2)       # a.gd + Node 外部
        self.assertEqual(st["user_nodes"], 1)
        self.assertEqual(st["external_nodes"], 1)
        self.assertEqual(st["edges"], 1)
        self.assertEqual(st["skipped"], 0)
        for n in g["nodes"]:
            self.assertEqual(
                set(n),
                {"id", "label", "sub", "kind", "rel", "line", "region",
                 "region_name", "external", "doc"},
            )

    def test_gd_static_class_call_and_method_dedup(self):
        _write(self.root, "values/crit.gd", (
            "class_name CritConfig\n"
            "func crit_damage(base: float, mult: float) -> float:\n"
            "\treturn base * mult\n"
        ))
        _write(self.root, "behaviors/player.gd", (
            "extends CharacterBody3D\n"
            "func hit() -> void:\n"
            "\tCritConfig.crit_damage(1.0, 2.0)\n"
            "\tCritConfig.crit_damage(2.0, 2.0)\n"
        ))
        g = wb.build_relation_graph(self.root)
        calls = [e for e in g["edges"] if e["kind"] == "calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["source"], calls[0]["target"]),
                         ("gd:behaviors/player.gd", "gd:values/crit.gd"))
        self.assertEqual(calls[0]["methods"], ["crit_damage"])
        self.assertEqual(calls[0]["line"], 3)
        self.assertEqual(g["stats"]["edges_by_kind"]["calls"], 1)

    def test_gd_typed_field_and_parameter_calls(self):
        _write(self.root, "ui/hud.gd", (
            "class_name HUD\n"
            "func show_hp(hp: int, maximum: int) -> void:\n"
            "\tpass\n"
        ))
        _write(self.root, "values/crit.gd", (
            "class_name CritConfig\n"
            "func crit_damage() -> void:\n"
            "\tpass\n"
        ))
        _write(self.root, "behaviors/player.gd", (
            "extends CharacterBody3D\n"
            "@onready var hud: HUD = $HUD\n"
            "func take_damage(cfg: CritConfig) -> void:\n"
            "\thud.show_hp(1, 2)\n"
            "\tcfg.crit_damage()\n"
        ))
        g = wb.build_relation_graph(self.root)
        calls = {(e["source"], e["target"], e["line"]) for e in g["edges"] if e["kind"] == "calls"}
        self.assertIn(("gd:behaviors/player.gd", "gd:ui/hud.gd", 4), calls)
        self.assertIn(("gd:behaviors/player.gd", "gd:values/crit.gd", 5), calls)

    def test_gd_call_noise_and_self_call_are_excluded(self):
        _write(self.root, "values/cfg.gd", (
            "class_name Cfg\n"
            "func ping() -> void:\n"
            "\tpass\n"
        ))
        _write(self.root, "behaviors/player.gd", (
            "class_name Player\n"
            "func ping() -> void:\n"
            "\tmove_and_slide()\n"
            "\tprint(\"Cfg.ping()\")\n"
            "\t# Cfg.ping()\n"
            "\tping()\n"
        ))
        g = wb.build_relation_graph(self.root)
        self.assertEqual([e for e in g["edges"] if e["kind"] == "calls"], [])

    def test_python_project_call_and_external_or_missing_method_excluded(self):
        _write(self.root, "models.py", (
            "class Target:\n"
            "    def run(self):\n"
            "        pass\n"
        ))
        _write(self.root, "logic.py", (
            "from models import Target\n"
            "import os\n"
            "class Source:\n"
            "    def go(self, target: Target):\n"
            "        target.run()\n"
            "        target.missing()\n"
            "        os.path.join('a', 'b')\n"
        ))
        g = wb.build_relation_graph(self.root)
        calls = [e for e in g["edges"] if e["kind"] == "calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["source"], calls[0]["target"], calls[0]["methods"]),
                         ("py:logic.py:Source", "py:models.py:Target", ["run"]))

    def test_gd_local_var_type_does_not_leak_across_functions(self):
        # 回归：函数内缩进的局部 var 不能被收进类成员映射，
        # 否则后续函数里同名的未定义接收者会造出假调用边。
        _write(self.root, "values/a.gd", (
            "class_name A\n"
            "func foo() -> void:\n"
            "\tpass\n"
            "func bar() -> void:\n"
            "\tpass\n"
        ))
        _write(self.root, "behaviors/x.gd", (
            "class_name X\n"
            "var shared: A\n"
            "func one() -> void:\n"
            "\tvar a: A\n"
            "\ta.foo()\n"
            "func two() -> void:\n"
            "\ta.bar()\n"
            "\tshared.bar()\n"
        ))
        g = wb.build_relation_graph(self.root)
        calls = [e for e in g["edges"] if e["kind"] == "calls"]
        # 第 5 行局部变量边 + 第 8 行成员边聚合成一条；第 7 行未定义接收者无假边
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["source"], calls[0]["target"]),
                         ("gd:behaviors/x.gd", "gd:values/a.gd"))
        self.assertEqual(calls[0]["line"], 5)
        self.assertEqual(calls[0]["methods"], ["foo", "bar"])

    def test_gd_self_and_same_class_calls_are_self_loops_excluded(self):
        _write(self.root, "behaviors/player.gd", (
            "class_name Player\n"
            "func ping() -> void:\n"
            "\tself.ping()\n"
            "\tPlayer.ping()\n"
        ))
        g = wb.build_relation_graph(self.root)
        self.assertEqual([e for e in g["edges"] if e["kind"] == "calls"], [])

    def test_gd_multiple_methods_merge_keep_first_line(self):
        _write(self.root, "values/crit.gd", (
            "class_name CritConfig\n"
            "func a() -> void:\n"
            "\tpass\n"
            "func b() -> void:\n"
            "\tpass\n"
        ))
        _write(self.root, "behaviors/player.gd", (
            "extends Node\n"
            "func f() -> void:\n"
            "\tCritConfig.b()\n"
            "\tCritConfig.a()\n"
            "\tCritConfig.b()\n"
        ))
        g = wb.build_relation_graph(self.root)
        calls = [e for e in g["edges"] if e["kind"] == "calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["methods"], ["b", "a"])  # 按首次出现顺序
        self.assertEqual(calls[0]["line"], 3)             # 首个调用点

    def test_python_module_qualified_call(self):
        # import models 后 models.Target.run() 经模块别名解析到项目内类
        _write(self.root, "models.py", (
            "class Target:\n"
            "    def run(self):\n"
            "        pass\n"
        ))
        _write(self.root, "logic.py", (
            "import models\n"
            "class Source:\n"
            "    def go(self):\n"
            "        models.Target.run()\n"
            "        models.Target()\n"
        ))
        g = wb.build_relation_graph(self.root)
        calls = [e for e in g["edges"] if e["kind"] == "calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["source"], calls[0]["target"], calls[0]["methods"]),
                         ("py:logic.py:Source", "py:models.py:Target", ["run"]))


if __name__ == "__main__":
    unittest.main()
