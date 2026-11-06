"""System resource monitoring endpoints (read-only snapshots)."""
from fastapi import APIRouter


def build_router(ctx) -> APIRouter:
    router = APIRouter(prefix="/api/system", tags=["system"])

    @router.get("/resources")
    async def resources():
        """最近一次 CPU / 内存 / 磁盘 / GPU 采样 + 静态能力画像。

        采样在后台线程里按固定间隔进行，端只读内存快照（TTL 1.5s），
        因此响应时间与前端轮询频率无关，也不会把 CPU 自己拉高。
        """
        return ctx.system_resources.snapshot()

    return router


__all__ = ["build_router"]
