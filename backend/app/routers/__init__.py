"""业务路由汇总：固定路径专用路由先注册，通用模块资源路由兜底。"""
from __future__ import annotations

from app.routers import path_evidence, resource

ROUTERS = [path_evidence, resource]
