# -*- coding: utf-8 -*-
"""
Contract AI 网页服务入口（Web 版）
用法：python app.py  →  http://localhost:8000
等价于：python -m agent.app
真实实现位于 agent/app.py（FastAPI 应用与全部 API）。
"""
from agent.app import app  # noqa: F401  导入即构建 FastAPI 应用（含启动初始化）

if __name__ == "__main__":
    import uvicorn

    from core.config import LLM_MODEL_NAME
    from core.llm_provider import cloud_status

    _c = cloud_status()
    print("=" * 60)
    print("  Contract AI · 本地化智能合同审查系统（网页版 · 用户版）")
    print(f"  本地模型：{LLM_MODEL_NAME} ｜ 云端模型：{_c['model']}"
          f"{'（已配置）' if _c['ready'] else '（未配置 Key，可在网页端设置）'}")
    print("  服务：http://localhost:8000（右上角「模型设置」可切换 本地/云端）")
    print("  预置演示账号：demo / demo123（含保密协议等演示合同）")
    print("  Ctrl+C 退出")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
