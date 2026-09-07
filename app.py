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

    print("=" * 60)
    print("  Contract AI · 本地化智能合同审查系统（网页版 · 用户版）")
    print(f"  模型：{LLM_MODEL_NAME}   服务：http://localhost:8000")
    print("  预置演示账号：demo / demo123（含保密协议等演示合同）")
    print("  Ctrl+C 退出")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
