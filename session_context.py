# -*- coding: utf-8 -*-
"""
session_context.py —— 会话级「上下文合同范围」（网页版用）

记录每个会话（thread_id）当前选定的上下文合同文件集合：
  - 未记录 / None          → 全部合同（默认，不限制）
  - list[str]              → 仅将这些合同文件作为 Agent 检索依据

Web 端每次发起对话前调用 set_sources 登记；Agent 的工具（tools.py）在执行
检索类工具时按 thread_id 查询 get_sources，从而把 Chroma 检索范围限定在
用户所选合同内。命令行（main.py / agent_run.py）不设置任何会话，恒为「全部」。
"""
import threading

_lock = threading.Lock()
# thread_id -> list[str] | None   （None 表示全部合同）
_thread_sources: dict = {}
# thread_id -> 归属用户名 | None（Web 端必填；None=不隔离，命令行单用户用）
_thread_owner: dict = {}
_MAX_THREADS = 500


def set_sources(thread_id: str, sources):
    """记录某会话选定的上下文合同范围；sources 为空 / None 表示全部合同。"""
    with _lock:
        if sources:
            _thread_sources[thread_id] = [str(s) for s in sources]
        else:
            _thread_sources[thread_id] = None
        # 防内存无限增长：超出上限时清理最旧的 100 条
        if len(_thread_sources) > _MAX_THREADS:
            for k in list(_thread_sources)[:100]:
                _thread_sources.pop(k, None)
                _thread_owner.pop(k, None)


def set_owner(thread_id: str, owner):
    """记录某会话的归属用户（用于向量检索的用户隔离）。"""
    with _lock:
        _thread_owner[thread_id] = owner or None


def get_owner(thread_id: str):
    """返回某会话的归属用户（None = 不隔离）。"""
    if not thread_id:
        return None
    with _lock:
        return _thread_owner.get(thread_id)


def get_sources(thread_id: str):
    """返回某会话的上下文合同范围（None = 全部合同）。"""
    if not thread_id:
        return None
    with _lock:
        return _thread_sources.get(thread_id)


def clear(thread_id: str) -> None:
    """清除某会话的上下文选择（恢复为全部合同）。"""
    with _lock:
        _thread_sources.pop(thread_id, None)
