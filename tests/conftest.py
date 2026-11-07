"""测试级环境隔离（R11）。

## 为什么需要这个

本机 `.env` 的「实时音视频」段里配置了：

    DOCMIND_REALTIME_PROVIDER=dashscope_omni
    DASHSCOPE_API_KEY=sk-ws-...

`config.load_dotenv()` 会把它带进 `os.environ`，于是 `/api/vision/live-stream` 网关
**按配置走原生实时通道**。这对产品是对的（用户就是要原生实时），但它会让测试变得不确定：

1. 帧被直接转给 provider，不再产生 `video.observation` → 所有"发帧等观察"的测试
   **永久阻塞**（实测：整组 realtime 测试 600s 超时且无输出）；
2. `hello.ok.mode` 从 `sampled-frames` 变成 `native-realtime`，`capabilities` 也换成
   provider 的能力集合 → 断言抽帧能力的测试直接失败；
3. 每条连接都真的对外连一次实时服务（有网络往来与费用）。

测试要的是确定性，不是"跟着开发机的 `.env` 变"。所以这里统一把**模式开关**清掉，
让"没配 provider 就降级"成为默认前提。需要原生通道的测试自己显式设置
（见 `tests/test_realtime_gateway_bridge.py`，它注册一个假 provider，不碰真实服务）。

## 只清一个变量

只清 `DOCMIND_REALTIME_PROVIDER`。它是决定网关模式的唯一开关：
`realtime_provider.resolve()` 在没有 provider 名时会直接返回降级结果，根本不会走到
`availability()`。**不清 `DASHSCOPE_API_KEY`**——那是全项目共用的厂商 key，
普通 LLM 路径也在用，清掉会误伤与本改动无关的测试。
"""
from __future__ import annotations

import os

import pytest

# 决定网关走原生还是抽帧的唯一开关。
_MODE_SWITCH = "DOCMIND_REALTIME_PROVIDER"


def pytest_addoption(parser):
    """`pytest --live`：额外运行需要真实模型的记忆黄金 live 组（默认不跑）。"""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="额外运行 tests/golden/memory/cases/live 下需要真实模型的用例",
    )


@pytest.fixture(autouse=True)
def _isolate_realtime_provider_env():
    """让整个测试套件默认走兼容抽帧通道；测试结束后原样还原。"""
    saved = os.environ.pop(_MODE_SWITCH, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[_MODE_SWITCH] = saved
