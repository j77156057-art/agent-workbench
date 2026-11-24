"""Game-engine discovery, lifecycle, embedding and bridge endpoints.

原先 build_router 是一个 248 行的巨型函数（38 个端点 + 一个动态注册循环）。
现按**连续路由段**拆成若干个 _register_* 函数：

- 端点代码逐字搬运，路由注册顺序与拆分前完全一致；
- 模块级名字一个都没动（测试直接 patch 过它们）。

"""
import json
import urllib.parse
import urllib.request

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool


class EngineReq(BaseModel):
    executable: str = "godot"
    scene: str = ""
    engine: str = "godot"
    embed: bool = False
    host_hwnd: int = 0
    rect: dict = {}
    fill: bool = False


class EngineEmbedReq(BaseModel):
    host_hwnd: int = 0
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    offset_y: int = -1
    title_hint: str = ""
    fill: bool = False


class EngineFocusReq(BaseModel):
    keep_attached: bool = False


class EnginePlaceReq(BaseModel):
    x: int
    y: int
    width: int
    height: int


class UnrealBridgeReq(BaseModel):
    confirm: bool = False
    force: bool = False


class UnrealWriteReq(BaseModel):
    task_id: str = ""
    target_path: str = ""
    property: str = ""
    value: object = None
    confirm: bool = False


class EngineDiagnosticsReq(BaseModel):
    engine: str = "unreal"
    text: str = ""


class GodotCheckReq(BaseModel):
    path: str
    executable: str = ""
    timeout: int = 120


class GodotAddonInstallReq(BaseModel):
    confirm: bool = False
    force: bool = False


class WebTemplateInstallReq(BaseModel):
    confirm: bool = False


async def engine_start_endpoint(ctx, req: EngineReq):
    root = ctx._project_root_or_error()
    if not root:
        return {"ok": False, "error": "未配置代码库"}
    host = req.host_hwnd or ctx._desktop_host_for(ctx._request_project_id())
    rect = req.rect if (req.rect or {}).get("width") and (req.rect or {}).get("height") else None
    stopped, warnings = await ctx._stop_other_engines(root)
    result = await run_in_threadpool(
        ctx.engine_start, root, req.executable, req.scene, host, req.embed, rect, req.fill)
    if isinstance(result, dict):
        notices = []
        if stopped:
            notices.append("已自动停止其它项目的引擎：" + "、".join(stopped))
        notices.extend(warnings)
        if result.get("notice"):
            notices.append(result["notice"])
        if notices:
            result = {**result, "notice": "；".join(notices)}
            if stopped or warnings:
                result["auto_stopped_roots"] = stopped
    return result


async def unreal_write_endpoint(_ctx, req: UnrealWriteReq):
    if not req.confirm:
        return JSONResponse({"ok": False, "error": "写入 Unreal 属性需要显式确认。"}, status_code=400)
    if (not req.task_id or not req.target_path
            or req.target_path.lower().endswith((".uasset", ".umap"))):
        return JSONResponse({"ok": False,
                             "error": "缺少任务范围，或禁止直接写入二进制 Unreal 资产。"},
                            status_code=400)
    return {"ok": False, "available": False,
            "error": "当前 bridge 仅支持查询，尚未启用安全写回。"}


async def _bridge_get(path: str, url: str):
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=5) as response:
            return {"ok": True, "available": True,
                    **json.loads(response.read().decode())}
    except Exception as exc:
        return {"ok": True, "available": False, "error": str(exc)}


def _godot_executable(ctx, root):
    config = ctx.engine_config(root)
    if config.get("engine", "godot") != "godot":
        return None
    return ctx._resolve_engine_executable("godot", config.get("executable", "godot"))


def _register_discovery(router, ctx):
    """发现与准备：试玩、状态、目录、扫描、检查、Unity 图、准备。"""
    root_or_error = ctx._project_root_or_error
    @router.post("/api/playtest")
    async def playtest(req: ctx.CommandReq):
        root = root_or_error()
        return (ctx.playtest(root, req.command, req.timeout) if root
                else {"ok": False, "error": "未配置代码库"})

    @router.get("/api/engine/status")
    async def status():
        root = root_or_error()
        return ctx.engine_status(root) if root else {"ok": False, "error": "未配置代码库"}

    @router.get("/api/engine/catalog")
    async def catalog():
        return ctx.engine_catalog()

    @router.get("/api/engine/scan")
    async def scan():
        root = root_or_error()
        return ctx.engine_scan(root) if root else {"ok": False, "error": "未配置代码库"}

    @router.get("/api/engine/inspect")
    async def inspect(engine: str = ""):
        root = root_or_error()
        return ctx.engine_inspect(root, engine) if root else {"ok": False, "error": "未配置代码库"}

    @router.get("/api/unity/guid-graph")
    async def unity_graph():
        root = root_or_error()
        return (await run_in_threadpool(ctx.unity_graph.build_unity_graph, root)
                if root else {"ok": False, "error": "未配置代码库"})

    @router.post("/api/engine/prepare")
    async def prepare(req: EngineReq):
        root = root_or_error()
        return (ctx.engine_prepare(root, req.engine, req.executable) if root
                else {"ok": False, "error": "未配置代码库"})


def _register_unreal(router, ctx):
    """Unreal 桥接：安装、状态、资产、角色、蓝图、读角色、写文件。"""
    root_or_error = ctx._project_root_or_error
    @router.post("/api/engine/unreal-bridge/install")
    async def install_unreal(req: UnrealBridgeReq):
        root = root_or_error()
        if not root:
            return {"ok": False, "error": "未配置代码库"}
        if not req.confirm:
            return JSONResponse({"ok": False, "error": "安装 Unreal 桥接脚本需要明确确认。"}, status_code=400)
        return ctx.install_unreal_bridge(root, req.force)

    @router.get("/api/engine/unreal-bridge/status")
    async def unreal_status(url: str = "http://127.0.0.1:8765"):
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/", timeout=2) as response:
                data = json.loads(response.read().decode())
            return {"ok": True, "available": True, "bridge": data}
        except Exception as exc:
            return {"ok": True, "available": False, "error": str(exc)}

    @router.get("/api/engine/unreal-bridge/assets")
    async def unreal_assets(url: str = "http://127.0.0.1:8765"):
        return await _bridge_get("/assets", url)

    @router.get("/api/engine/unreal-bridge/actors")
    async def unreal_actors(url: str = "http://127.0.0.1:8765"):
        return await _bridge_get("/actors", url)

    @router.get("/api/engine/unreal-bridge/blueprint/{asset_path:path}")
    async def unreal_blueprint(asset_path: str, url: str = "http://127.0.0.1:8765"):
        return await _bridge_get("/blueprint/" + urllib.parse.quote(asset_path, safe=""), url)

    @router.get("/api/engine/unreal-bridge/actor/{actor_name:path}")
    async def unreal_actor(actor_name: str, url: str = "http://127.0.0.1:8765"):
        return await _bridge_get("/actor/" + urllib.parse.quote(actor_name, safe=""), url)

    @router.post("/api/engine/unreal-bridge/write")
    async def unreal_write(req: UnrealWriteReq):
        return await unreal_write_endpoint(ctx, req)


def _register_skillconfig(router, ctx):
    """技能与配置：技能解析、读配置、写配置。"""
    root_or_error = ctx._project_root_or_error
    @router.get("/api/engine/skill")
    async def skill(engine: str = "godot"):
        if engine not in {"godot", "unity", "unreal"}:
            return JSONResponse({"ok": False, "error": "不支持的引擎。"}, status_code=400)
        return {"ok": True, "engine": engine, "skill": ctx.skill_for_engine(engine)}

    @router.get("/api/engine/config")
    async def get_config():
        root = root_or_error()
        return ctx.engine_config(root) if root else {"ok": False, "error": "未配置代码库"}

    @router.post("/api/engine/config")
    async def set_config(req: EngineReq):
        root = root_or_error()
        return (ctx.engine_config(root, req.engine, req.executable) if root
                else {"ok": False, "error": "未配置代码库"})


def _register_runtime(router, ctx):
    """运行时控制：启动、停止、重载、变更、嵌入、放置、分离、聚焦、缩放、日志、探针、校验、诊断、检查。"""
    root_or_error = ctx._project_root_or_error
    @router.post("/api/engine/start")
    async def start(req: EngineReq):
        return await engine_start_endpoint(ctx, req)

    @router.post("/api/engine/stop")
    async def stop():
        root = root_or_error()
        return (await run_in_threadpool(ctx.engine_stop, root)
                if root else {"ok": False, "error": "未配置代码库"})

    @router.post("/api/engine/reload")
    async def reload():
        root = root_or_error()
        return (await run_in_threadpool(ctx.engine_reload, root)
                if root else {"ok": False, "error": "未配置代码库"})

    @router.get("/api/engine/changes")
    async def changes():
        root = root_or_error()
        return ctx.engine_changes(root) if root else {"ok": False, "error": "未配置代码库"}

    @router.post("/api/engine/embed")
    async def embed(req: EngineEmbedReq):
        root = root_or_error()
        if not root:
            return {"ok": False, "error": "未配置代码库"}
        host = req.host_hwnd or ctx._desktop_host_for(ctx._request_project_id())
        offset = ctx.EMBED_TOP_STRIP if req.offset_y < 0 else req.offset_y
        rect = ({"x": req.x, "y": req.y, "width": req.width, "height": req.height}
                if req.width > 0 and req.height > 0 else None)
        return await run_in_threadpool(
            ctx.engine_embed, root, host, req.width or None, req.height or None,
            req.title_hint, offset, rect, req.fill)

    @router.post("/api/engine/place")
    async def place(req: EnginePlaceReq):
        root = root_or_error()
        return (await run_in_threadpool(ctx.engine_place, root, req.x, req.y, req.width, req.height)
                if root else {"ok": False, "error": "未配置代码库"})

    @router.post("/api/engine/detach")
    async def detach():
        root = root_or_error()
        return (await run_in_threadpool(ctx.engine_detach, root)
                if root else {"ok": False, "error": "未配置代码库"})

    @router.post("/api/engine/focus")
    async def focus(req: EngineFocusReq):
        root = root_or_error()
        return (await run_in_threadpool(ctx.engine_focus, root, req.keep_attached)
                if root else {"ok": False, "error": "未配置代码库"})

    @router.post("/api/engine/resize")
    async def resize(offset_y: int = -1):
        root = root_or_error()
        return (await run_in_threadpool(ctx.engine_resize, root, None if offset_y < 0 else offset_y)
                if root else {"ok": False, "error": "未配置代码库"})

    @router.get("/api/engine/logs")
    async def logs(limit: int = 200):
        root = root_or_error()
        return ctx.engine_logs(root, limit) if root else {"ok": False, "error": "未配置代码库"}

    @router.post("/api/runtime/probe")
    async def runtime_probe():
        root = root_or_error()
        return ctx.install_runtime_probe(root) if root else {"ok": False, "error": "未配置代码库"}

    @router.post("/api/engine/verify")
    async def verify(req: EngineReq):
        root = root_or_error()
        return ctx.engine_verify(root, req.executable) if root else {"ok": False, "error": "未配置代码库"}

    @router.post("/api/engine/diagnostics")
    async def diagnostics(req: EngineDiagnosticsReq):
        if req.engine.lower() != "unreal":
            return {"ok": False, "error": "当前仅支持 Unreal 诊断解析。"}
        return {"ok": True, "engine": "unreal",
                "diagnostics": ctx.parse_unreal_diagnostics(req.text)}

    @router.post("/api/engine/check")
    async def check(req: GodotCheckReq):
        root = root_or_error()
        return (await run_in_threadpool(ctx.godot_check_script, root, req.path,
                                        req.executable, req.timeout)
                if root else {"ok": False, "error": "未配置代码库"})


def _register_addonweb(router, ctx):
    """Godot 插件与网页导出：插件状态、安装、网页模板、安装模板、导出。"""
    root_or_error = ctx._project_root_or_error
    @router.get("/api/engine/addon/status")
    async def addon_status():
        root = root_or_error()
        return ctx.godot_addon_status(root) if root else {"ok": False, "error": "未配置代码库"}

    @router.post("/api/engine/addon/install")
    async def addon_install(req: GodotAddonInstallReq):
        if not req.confirm:
            return JSONResponse({"ok": False, "error": "安装 godot-ai 插件需要用户明确确认。"}, status_code=400)
        root = root_or_error()
        return (await run_in_threadpool(ctx.install_godot_addon, root, req.force)
                if root else {"ok": False, "error": "未配置代码库"})

    @router.get("/api/engine/web/templates")
    async def web_templates():
        root = root_or_error()
        if not root:
            return {"ok": False, "error": "未配置代码库"}
        executable = _godot_executable(ctx, root)
        if not executable:
            return {"ok": False, "error": "当前引擎不是 Godot 或未找到可执行文件。"}
        return await run_in_threadpool(ctx.web_export.templates_status, executable)

    @router.post("/api/engine/web/templates/install")
    async def install_web_templates(req: WebTemplateInstallReq):
        if not req.confirm:
            return JSONResponse({"ok": False, "error": "下载 Godot Web 导出模板需要用户明确确认。"}, status_code=400)
        root = root_or_error()
        if not root:
            return {"ok": False, "error": "未配置代码库"}
        executable = _godot_executable(ctx, root)
        if not executable:
            return {"ok": False, "error": "当前引擎不是 Godot 或未找到可执行文件。"}
        started = await run_in_threadpool(ctx.web_export.install_templates_async, executable)
        return {"ok": True, "started": started, "install": ctx.web_export.install_state()}

    @router.post("/api/engine/web/export")
    async def export_web():
        root = root_or_error()
        if not root:
            return {"ok": False, "error": "未配置代码库"}
        executable = _godot_executable(ctx, root)
        if not executable:
            return {"ok": False, "error": "当前引擎不是 Godot 或未找到可执行文件。"}
        return await run_in_threadpool(ctx.web_export.export_web, root, executable, 300)


def build_router(ctx) -> APIRouter:
    """装配引擎路由族（拆分说明见本模块顶部）。"""
    router = APIRouter(tags=["engine"])
    _register_discovery(router, ctx)
    _register_unreal(router, ctx)
    _register_skillconfig(router, ctx)
    _register_runtime(router, ctx)
    _register_addonweb(router, ctx)
    for path, method, name in [
        ("/api/tasks", "GET", "tasks_ep"),
        ("/api/tasks/{task_id}", "GET", "task_get_ep"),
        ("/api/tasks", "POST", "task_upsert_ep"),
        ("/api/tasks/validate", "POST", "task_validate_ep"),
        ("/api/tasks/impact", "POST", "task_impact_ep"),
        ("/api/tasks/snapshot", "POST", "task_snapshot_ep"),
        ("/api/tasks/verify", "POST", "task_verify_ep"),
        ("/api/tasks/revert", "POST", "task_revert_ep"),
        ("/api/tasks/branch", "POST", "task_branch_ep"),
        ("/api/validate_data", "GET", "validate_data_ep"),
        ("/api/localization_check", "GET", "localization_ep"),
        ("/api/release_check", "GET", "release_ep"),
        ("/api/simulate_growth", "GET", "simulate_ep"),
    ]:
        router.add_api_route(path, getattr(ctx, name), methods=[method])
    return router
    return router


__all__ = [name for name in globals() if name.endswith("Req")]
