# -*- coding: utf-8 -*-
"""
tools.py —— 自定义工具集（供 Agent 自主调用）
使用 @tool 装饰器把系统能力封装为 LLM 可决策调用的工具：
  1. search_contract_knowledge  知识库检索问答（RAG Tool）
  2. analyze_contract           合同多维度智能审查
  3. extract_contract_elements  合同关键要素结构化抽取
  4. list_contract_files        查看库内可用合同清单
"""
import os

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

import bootstrap
import session_context
from contract_analyzer import analyze_contract, _list_contract_files
from contract_kb import kb_query
from element_extractor import extract_elements
from storage import get_store


def _thread_ctx(config: RunnableConfig):
    """从工具运行时 config 取 thread_id，查该会话的 (归属用户, 合同范围)。"""
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not thread_id:
        return None, None
    return (session_context.get_owner(thread_id),
            session_context.get_sources(thread_id))


def _owner_files(owner: str) -> list:
    """当前账户合同库的文件名清单（供“未找到”时的友好提示）。"""
    if not owner:
        return []
    try:
        u = get_store().get_user_by_name(owner)
        return [f["name"] for f in get_store().list_files(u["id"])]
    except Exception:  # noqa: BLE001
        return []


def _resolve_agent_path(contract_name: str, owner: str):
    """解析当前会话用户合同库内的文件绝对路径。
    - owner 非空（网页登录场景）：在 uploads/<owner>/ 下解析（找不到返回 None）
    - owner 为空（命令行场景）：原样返回名称，交给 analyze/extract 的 contracts/ 逻辑
    """
    if owner:
        return bootstrap.resolve_user_file(owner, contract_name)
    return contract_name


@tool
def search_contract_knowledge(query: str, config: RunnableConfig) -> str:
    """
    检索企业内部合同知识库并基于合同原文回答问题。
    当用户询问“合同里怎么约定的 / 某条款是什么 / 知识库中的合同内容”时必须调用本工具。
    注意：检索范围仅限当前会话选定的上下文合同；若该范围内无相关内容，如实告知用户，不要编造。
    Args:
        query: 用户的合同相关问题
    """
    owner, sources = _thread_ctx(config)
    answer, docs = kb_query(query, top_k=3, sources=sources, owner=owner)
    if not docs:
        return "知识库未检索到相关合同内容，可能尚未入库或不在当前上下文合同范围内。"
    lines = [f"【回答】{answer}", "【引用来源】"]
    seen = set()
    for d in docs:
        s = d.metadata.get("source", "未知")
        if s not in seen:
            seen.add(s)
            lines.append(f"  - {s}")
    return "\n".join(lines)


@tool
def analyze_contract_tool(contract_name: str, focus_dimensions: str = "",
                          config: RunnableConfig = None) -> str:
    """
    对当前账户合同库中的指定合同执行多维度智能风险审查，输出风险等级与修改建议。
    当用户要求“审查/分析/检查某合同的风险”时必须调用本工具。
    如不确定合同文件名，请先调用 list_contract_files_tool 获取当前可用合同清单。
    Args:
        contract_name: 当前账户合同库中的合同文件名（如：保密协议_sample.txt，可只写部分名称）
        focus_dimensions: 重点关注的风险维度，逗号分隔，可留空（默认全部维度）
    """
    owner, _sources = _thread_ctx(config)
    path = _resolve_agent_path(contract_name, owner)
    if path is None:
        files = _owner_files(owner)
        hint = "\n".join(f"- {f}" for f in files) if files else "（空）"
        return (f"你的合同库中未找到《{contract_name}》。当前可用合同：\n{hint}\n"
                f"请用准确的合同文件名重试。")
    dims = [d.strip() for d in focus_dimensions.split(",") if d.strip()] or None
    try:
        # 被 Agent 调用时静默执行（stream=False），结果由 Agent 汇总返回
        results = analyze_contract(path, dims=dims, progress=False, stream=False,
                                   owner=owner)
    except Exception as e:  # noqa: BLE001
        return f"审查失败：{e}"
    lines = [f"《{os.path.basename(path)}》审查结果："]
    for r in results:
        lines.append(
            f"- 【{r.get('dimension', '')}】风险:{r.get('risk_level', '未知')} | "
            f"依据:{str(r.get('evidence', ''))[:80]} | 意见:{r.get('opinion', '')}"
        )
    return "\n".join(lines)


@tool
def extract_contract_elements_tool(contract_name: str,
                                   config: RunnableConfig = None) -> str:
    """
    抽取当前账户合同库中合同的关键要素（类型/当事人/金额/期限/日期/争议解决等），
    输出结构化信息。当用户要求“总结/提取某合同的关键信息、当事人、金额、期限”时调用。
    如不确定合同文件名，请先调用 list_contract_files_tool 获取当前可用合同清单。
    Args:
        contract_name: 当前账户合同库中的合同文件名（如：保密协议_sample.txt，可只写部分名称）
    """
    owner, _sources = _thread_ctx(config)
    path = _resolve_agent_path(contract_name, owner)
    if path is None:
        files = _owner_files(owner)
        hint = "\n".join(f"- {f}" for f in files) if files else "（空）"
        return (f"你的合同库中未找到《{contract_name}》。当前可用合同：\n{hint}\n"
                f"请用准确的合同文件名重试。")
    try:
        # 被 Agent 调用时静默执行（stream=False）
        data = extract_elements(path, stream=False)
    except Exception as e:  # noqa: BLE001
        return f"抽取失败：{e}"
    lines = [f"《{os.path.basename(path)}》关键要素："]
    for k, v in data.items():
        if v:
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)


@tool
def list_contract_files_tool(config: RunnableConfig) -> str:
    """列出当前可用的合同文件。当用户问“有哪些合同/有什么文件”时调用。"""
    owner, sources = _thread_ctx(config)
    if sources:
        return ("当前上下文合同范围（仅以下文件作为检索依据）：\n"
                + "\n".join(f"- {s}" for s in sources))
    if owner:
        return f"当前账户（{owner}）的合同库文件：\n" + "\n".join(
            f"- {f['name']}" for f in get_store().list_files(
                get_store().get_user_by_name(owner)["id"]))
    files = _list_contract_files()
    if not files:
        return "合同目录为空，尚未导入任何合同。"
    return "当前合同库文件：\n" + "\n".join(f"- {f}" for f in files)


# Agent 可用的全部工具清单
ALL_TOOLS = [
    search_contract_knowledge,
    analyze_contract_tool,
    extract_contract_elements_tool,
    list_contract_files_tool,
]
