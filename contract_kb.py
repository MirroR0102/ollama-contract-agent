# -*- coding: utf-8 -*-
"""
contract_kb.py —— 合同知识库 RAG 问答
本地 LLM（ChatOllama）严格依据向量库检索到的合同片段作答，
禁止编造幻觉，并返回引用片段供核对。
"""
import time

from langchain_core.messages import HumanMessage

from ollama_conn import (BASE_DELAY, MAX_RETRY, get_llm, is_conn_error,
                         reset_llm, retry_emb_call)
from vector_store import get_retriever

# 无幻觉提示词模板（答辩重点：对比“直接问模型” vs “RAG 问答”）
_KB_PROMPT = """你是企业合同知识库问答助手，必须严格遵守：
1. 只能依据下方提供的【合同片段】回答问题；
2. 禁止编造、推测片段中不存在的内容；片段不足以回答时，直接说明“知识库合同未涉及该内容”；
3. 回答时尽量保留原条款的关键表述，并说明依据来源文件。

【合同片段】
{context}

【用户问题】
{question}
"""


def _format_context(docs) -> str:
    """把检索到的切片拼成带来源的上下文。"""
    parts = []
    for i, doc in enumerate(docs, 1):
        source = doc.metadata.get("source", "未知文件")
        parts.append(f"【片段{i}｜来源：{source}】\n{doc.page_content}")
    return "\n\n".join(parts)


def stream_generate(prompt: str, echo: bool = True, emit=None) -> str:
    """
    流式生成核心函数：用 llm.stream 逐块生成（连接失效自动重建重试）。
    - echo=True：边生成边打印（打字机效果，命令行/演示用）
    - emit：可选回调 emit(text片段)，供 Web 端逐 token 转发（SSE）
    返回完整文本。

    自愈说明：Ollama 中途重启导致连接失效时，请求在首个 token 前就会抛连接
    错误（此时尚未 emit 任何内容），本函数自动重建连接实例并从头重试；若在
    生成中途断连（Ollama 运行中崩溃），重试会重新输出全文——已 emit 的预览
    无法回滚，但最终以返回值（完整文本）为准，不影响结果正确性。
    """
    for attempt in range(MAX_RETRY + 1):
        collected: list[str] = []
        try:
            for chunk in get_llm().stream([HumanMessage(content=prompt)]):
                piece = chunk.content or ""
                if piece:
                    if echo:
                        print(piece, end="", flush=True)
                    if emit is not None:
                        emit(piece)
                    collected.append(piece)
            if echo:
                print()  # 收尾换行
            return "".join(collected)
        except Exception as e:  # noqa: BLE001
            if attempt < MAX_RETRY and is_conn_error(e):
                time.sleep(BASE_DELAY * (attempt + 1))
                reset_llm()
                continue
            raise
    raise RuntimeError("unreachable")


def kb_query(user_query: str, top_k: int = 3, emit=None, sources: list = None,
              owner: str = None) -> tuple:
    """
    知识库 RAG 问答（内部供工具/Agent 调用，静默收集不打印）。
    - emit：可选回调，逐 token 转发（Web 端 SSE 用）
    - sources：可选，仅在这些合同文件范围内检索；None/空 = 全部合同
    - owner：归属用户名（Web 端必传，用于用户隔离）；None = 不限制（命令行单用户用）
    返回：(回答文本, 检索到的切片列表)；检索为空时回答为提示语。
    """
    docs = retry_emb_call(
        lambda: get_retriever(top_k, sources=sources, owner=owner).invoke(user_query)
    )
    if not docs:
        return "知识库中未检索到相关合同内容，请先入库合同文档。", []

    prompt = _KB_PROMPT.format(context=_format_context(docs), question=user_query)
    answer = stream_generate(prompt, echo=False, emit=emit)  # Agent 场景不打印，避免干扰对话流
    return answer, docs


def show_rag_demo(question: str, top_k: int = 3) -> None:
    """演示用：打印“检索片段 + 模型流式回答”，便于答辩展示 RAG 过程。"""
    print("=" * 70)
    print(f"【用户问题】{question}\n")

    retriever = get_retriever(top_k)
    docs = retriever.invoke(question)
    if not docs:
        print("知识库中未检索到相关合同内容，请先入库合同文档。")
        return

    print("--- ① 检索到的合同片段（证据） ---")
    for i, doc in enumerate(docs, 1):
        print(f"[片段{i}｜{doc.metadata.get('source', '未知')}]")
        print(doc.page_content[:180].replace("\n", " "))
        print()
    print("--- ② 模型回答（流式生成中） ---")

    prompt = _KB_PROMPT.format(context=_format_context(docs), question=question)
    stream_generate(prompt)  # 打字机效果流式输出

    print()
    print("=" * 70)


if __name__ == "__main__":
    # 自检：直接测试一个知识库问题（需先入库）
    show_rag_demo("合同中的违约金是怎么约定的？")
