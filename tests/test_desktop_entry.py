# -*- coding: utf-8 -*-
"""桌面壳入口页 + 两个页面互跳的护栏。

这是一条**产品决策**，不是实现细节，所以要用测试钉住，防止被后人无意改回去：

* 默认首页 = **RAG 问答页 `/`**：它才是产品原点入口（"点开就能问"）；
* **开发工作台 `/workbench`** 是第二个入口（分区治理 / 引擎试玩 / 场景画布 / 运行时时间线）；
* 两页之间**必须能互跳**——不然进了工作台就出不来，或者根本找不到工作台。

历史：桌面壳曾经硬编码打开 `/workbench/`，而浏览器回退路径打开的是 `/`，
同一份东西"两条路进不同页面"，用户直接看懵过。
"""
import importlib
import os
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse_first_href(html: str):
    """取页面里第一个指向工作台的 href（`/workbench` 或 `/workbench.html`）。"""
    m = re.search(r'href="(/workbench(?:\.html)?)"', html)
    return m.group(1) if m else None


class DesktopEntryTests(unittest.TestCase):
    def test_home_path_defaults_to_question_page(self):
        import desktop
        self.assertEqual(desktop.HOME_PATH, '/',
                         '桌面壳默认入口必须是 RAG 问答页 `/`；'
                         '要换默认入口请用 DOCMIND_HOME 环境变量，不要改这里的默认值')

    def test_home_path_is_overridable_by_env(self):
        """允许不改代码切换默认入口（打包/自检时有用）。"""
        import desktop
        original = os.environ.get('DOCMIND_HOME')
        try:
            os.environ['DOCMIND_HOME'] = '/workbench'
            importlib.reload(desktop)
            self.assertEqual(desktop.HOME_PATH, '/workbench')
        finally:
            if original is None:
                os.environ.pop('DOCMIND_HOME', None)
            else:
                os.environ['DOCMIND_HOME'] = original
            importlib.reload(desktop)

    def test_host_window_uses_home_path(self):
        """宿主窗口不能再硬编码页面路径——必须走 HOME_PATH。"""
        with open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertNotIn('page = base + "/workbench/"', src,
                         'build_host_window 又硬编码了 /workbench/，与 HOME_PATH 脱节')
        self.assertIn('page = base + HOME_PATH', src)

    def test_question_page_links_to_workbench(self):
        path = os.path.join(ROOT, 'web', 'index.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        href = _parse_first_href(html)
        self.assertIsNotNone(href,
                             'RAG 问答页（默认首页）里没有通往开发工作台的链接，用户找不到工作台')
        self._assert_link_opens(href, '问答页「代码工作台 →」链接指向的路径必须能被后端打开（曾把 /workbench.html 写成死链导致 404）')

    def test_trace_page_links_to_workbench(self):
        path = os.path.join(ROOT, 'web', 'trace.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        href = _parse_first_href(html)
        self.assertIsNotNone(href, 'trace 页里没有通往开发工作台的链接')
        self._assert_link_opens(href, 'trace 页指向工作台的链接必须能被后端打开')

    def _assert_link_opens(self, href: str, msg: str):
        from fastapi.testclient import TestClient
        import api
        # 不进 with：不触发 lifespan（HANDOFF §8 第 32 条——校验脚本靠这条区别工作）。
        client = TestClient(api.app)
        resp = client.get(href, follow_redirects=True)
        self.assertEqual(resp.status_code, 200, f'{msg}（实际 {href} → HTTP {resp.status_code}）')

    def test_workbench_html_alias_redirects(self):
        """两个 .html 别名都应 307 到无后缀路由，最终 200。"""
        from fastapi.testclient import TestClient
        import api
        client = TestClient(api.app)
        for alias, target in (('/workbench.html', '/workbench'), ('/trace.html', '/trace')):
            r = client.get(alias, follow_redirects=False)
            self.assertEqual(r.status_code, 307, f'{alias} 应 307')
            self.assertEqual(r.headers.get('location'), target, f'{alias} 的 Location 应指向 {target}')
        for path in ('/workbench', '/workbench.html', '/trace', '/trace.html'):
            self.assertEqual(client.get(path, follow_redirects=True).status_code, 200,
                             f'{path} 应最终 200')

    def test_missing_index_page_returns_actionable_error(self):
        """未构建前端时 `/` 必须给出构建指引，不能 500。

        空白机器按 README 先起后端、后构建前端时，默认入口 `/` 会先被访问；
        旧实现直接 FileResponse 一个不存在的文件，浏览器看到的是一屏
        Internal Server Error，而隔壁 `/workbench` 早有明确提示。
        """
        from fastapi.testclient import TestClient
        import api
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(api, "PROJECT_WEB_DIR", tmp):
                resp = TestClient(api.app).get("/")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("npm run build", resp.json().get("error", ""))

    def test_workbench_links_back_to_question_page(self):
        path = os.path.join(ROOT, 'frontend', 'src', 'workbench', 'App.vue')
        with open(path, encoding='utf-8') as f:
            vue = f.read()
        # 顶栏那个回链的 class 现在叫 `wb-ask-link`（早先叫 `wb-question-link`，
        # 改名时前端改了、断言没跟上 —— 这条因此在 ubuntu 上一直红）。
        self.assertIn('wb-ask-link', vue, '工作台顶栏缺少回 RAG 问答页的入口')
        self.assertIn('href="/"', vue, '回链地址不对，应指向问答页 `/`')


if __name__ == '__main__':
    unittest.main()
