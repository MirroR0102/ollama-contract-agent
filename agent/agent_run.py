# -*- coding: utf-8 -*-
"""
agent_run.py —— 带记忆的合同审查 Agent（LangGraph 实现）
- 使用 LangChain 1.0 标准 create_agent 构建工具型智能体
- MemorySaver 实现多轮对话短期上下文记忆（thread_id 隔离不同会话）
- Agent 自主判断何时调用工具（知识库检索 / 合同审查 / 要素抽取 / 文件清单）
- 全程本地 Ollama 推理，断网可用
"""
from langchain.agents import create_agent
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessageChunk,
    HumanMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import MemorySaver

from agent.tools import ALL_TOOLS
from core.config import LLM_MODEL_NAME
from core.ollama_conn import get_llm

# 初始化会话记忆检查点（短期上下文记忆：同一 thread 记住多轮对话）
memory = MemorySaver()

# 系统提示词：约束 Agent 涉合同必须先查证再作答
_SYSTEM_PROMPT = (
    "你是企业智能合同助手，负责合同知识库问答、合同风险审查与要素抽取。\n"
    "行为准则：\n"
    "1. 用户询问合同内容、条款约定时，必须先调用 search_contract_knowledge 检索知识库再回答，禁止凭记忆编造；\n"
    "2. 用户要求审查/分析合同风险时，调用 analyze_contract_tool；\n"
    "3. 用户要求提取合同关键信息时，调用 extract_contract_elements_tool；\n"
    "4. 用户询问有哪些合同时，调用 list_contract_files_tool；\n"
    "5. 回答尽量给出依据（来源文件/原文片段），语言简洁专业；\n"
    "6. 禁止输出“我将调用…”“请稍候”“让我查找…”等过渡语：需要检索或工具时直接发起工具调用，"
    "取得工具结果后再给出完整回答；确无工具可解时如实说明；\n"
    "7. 最终回答要精炼：概述要点并引用条款编号/关键句即可，不要整段复述工具返回的原文，避免重复；\n"
    "8. 工具选用规则：问“有多少合同/切片/知识库规模”用 get_kb_stats_tool；问某合同详情/备注/大小"
    "用 get_contract_info_tool；按文件夹查看合同库用 list_folders_tool；要“精确查看某关键词在原文"
    "出现的位置/原文段落”用 locate_clause_tool；要求总结/概括某份合同整体内容时必须调用 "
    "summarize_contract_tool 通读全文后再总结（禁止只凭对话记忆或模板总结）；要求对比/比较两份合同"
    "时必须调用 compare_contracts_tool。\n"
)

_agent = None


def build_agent(system_prompt: str | None = None):
    """创建标准 LangChain Agent（绑定最新 LLM 连接 + 全局多轮记忆）。
    可传入动态 system_prompt（如按会话注入“当前处理对象”范围）。
    """
    return create_agent(
        model=get_llm(),
        tools=ALL_TOOLS,
        system_prompt=system_prompt or _SYSTEM_PROMPT,
        checkpointer=memory,   # 真正的多轮记忆：thread_id 隔离各会话
    )


def get_agent():
    """返回当前 Agent 实例（惰性创建；连接失效时由 reset_agent 重建）。"""
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


def reset_agent():
    """重建默认 Agent（LLM 连接失效时调用，绑定全新连接后可无损重试整轮）。"""
    global _agent
    _agent = build_agent()
    print("  [重连] 已重建 LangGraph Agent（绑定新 LLM 连接）", flush=True)
    return _agent


def chat_stream(user_input: str, thread_id: str = "user_001") -> str:
    """
    Agent 流式对话（打字机效果）：
    边推理边实时打印工具调用过程与回答 token，返回最终回答全文。
    """
    full_text: list[str] = []
    tool_names: dict = {}  # 按 index 累积工具名分片

    for chunk, metadata in get_agent().stream(
        {"messages": [HumanMessage(content=user_input)]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="messages",
    ):
        # 工具调用决策（模型节点流式输出 tool_call_chunks）
        tcc = getattr(chunk, "tool_call_chunks", None)
        if tcc:
            for tc in tcc:
                idx = tc.get("index", 0)
                name_piece = (tc.get("name") or "").strip()
                if name_piece:
                    # 工具名通常整段给出；只在第一次出现时提示调用
                    if idx not in tool_names:
                        tool_names[idx] = ""
                    tool_names[idx] += name_piece
                    if idx not in tool_names or len(tool_names[idx]) <= len(name_piece):
                        print(f"\n🤖 [调用工具] {name_piece}", end="", flush=True)

        text = chunk.content or ""
        if isinstance(chunk, AIMessageChunk):
            if text:
                print(text, end="", flush=True)
                full_text.append(text)
        elif isinstance(chunk, ToolMessage):
            # 工具执行结果（截断展示，避免刷屏）
            name = getattr(chunk, "name", "") or ""
            brief = str(text)[:180].replace("\n", " ")
            print(f"\n🔧 [工具返回] {name}: {brief}{'...' if len(text) > 180 else ''}", flush=True)

    print()  # 收尾换行
    return "".join(full_text)


def chat_once(user_input: str, thread_id: str = "user_001") -> str:
    """发送一轮对话（非交互场景用），返回最终回答文本。"""
    response = get_agent().invoke(
        {"messages": [HumanMessage(content=user_input)]},
        config={"configurable": {"thread_id": thread_id}},
    )
    # 返回最终回答
    for msg in reversed(response["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
    return str(response["messages"][-1].content)


def interactive(thread_id: str = "user_001") -> None:
    """交互式多轮对话（答辩主入口，全程流式）。输入 exit 退出。"""
    print("=" * 60)
    print(f"  🤖 合同智能体已就绪  |  模型：{LLM_MODEL_NAME}")
    print("  你可以问：条款内容 / 审查合同风险 / 抽取合同要素 / 有哪些合同")
    print("  输入 exit 退出对话")
    print("=" * 60)

    while True:
        user_input = input("\n👤 用户: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "退出"):
            print("对话结束，再见 👋")
            break

        print("\n🤖 AI: ", end="", flush=True)
        chat_stream(user_input, thread_id)


if __name__ == "__main__":
    interactive()
