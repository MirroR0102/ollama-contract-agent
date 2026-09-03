# -*- coding: utf-8 -*-
"""
contract_analyzer.py —— 合同智能审查引擎（核心亮点功能）
原理：对一份合同，按 8 个常见风险维度逐项“定向检索原文 + 大模型分析”，
每个维度先定位到合同中最相关的条款片段（降低幻觉），再输出
{风险等级 / 原文依据 / 审查意见}，最后按风险高低汇总成审查报告。

全程本地 Ollama 推理，可用于答辩演示“AI 辅助法务审查”。
"""
import os
import re

from langchain_core.messages import HumanMessage

from config import CONTRACTS_DIR, REVIEW_DIMENSIONS
from contract_kb import llm, stream_generate
from vector_store import add_file_to_kb, get_db

# 单维度检索片段数
_DIM_TOP_K = 2

# 每维度定向分析提示词
_DIM_PROMPT = """你是资深企业法务顾问，正在对一份合同做专项风险审查。
请只依据下面提供的【合同条款片段】进行判断，禁止编造条款中不存在的内容；
若片段中确实没有该维度相关约定，请如实给出“未约定”的风险提示。

【审查维度】{dim_name}
【审查关注点】{dim_desc}
【合同条款片段】
{context}

请严格按以下 JSON 格式输出（不要输出其他内容）：
{{
  "risk_level": "高/中/低/未约定",
  "evidence": "从片段中摘录的关键原文（无则填：未找到相关条款）",
  "opinion": "对该维度的风险分析与修改建议（40-100字）"
}}
"""


def _list_contract_files() -> list:
    """列出 contracts 目录下全部合同文件。"""
    if not os.path.isdir(CONTRACTS_DIR):
        return []
    return sorted(
        f for f in os.listdir(CONTRACTS_DIR)
        if f.lower().endswith((".txt", ".pdf"))
    )


def _resolve_file(name: str) -> str:
    """把用户输入解析为 contracts 目录内的绝对路径（支持模糊匹配文件名）。"""
    files = _list_contract_files()
    if not files:
        raise FileNotFoundError(f"合同目录 {CONTRACTS_DIR} 为空，请先放入合同文件")
    # 精确匹配
    if os.path.isfile(name):
        return os.path.abspath(name)
    # 文件名/部分名匹配
    hit = next((f for f in files if name in f or f in name), None)
    if hit:
        return os.path.abspath(os.path.join(CONTRACTS_DIR, hit))
    print("可用的合同文件：")
    for f in files:
        print(f"  - {f}")
    raise FileNotFoundError(f"未找到合同文件：{name}")


def _dimension_retriever(filename: str, top_k: int = _DIM_TOP_K):
    """构造“仅检索指定合同文件”的检索器（按 source 元数据过滤，避免串库）。"""
    db = get_db()
    return db.as_retriever(
        search_kwargs={
            "k": top_k,
            "filter": {"source": os.path.basename(filename)},
        }
    )


def _parse_json_response(text: str) -> dict:
    """从模型输出中容错解析 JSON（模型可能带 ```json 包裹或夹杂说明）。"""
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # 提取第一个 { ... } 块
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)
    try:
        import json

        return json.loads(text)
    except Exception:  # noqa: BLE001
        # 解析失败时返回原始文本，由上层兜底
        return {"raw": text, "risk_level": "未知", "opinion": text[:100]}


def analyze_dimension(filename: str, dim: dict, stream: bool = True, emit=None) -> dict:
    """对单个风险维度做定向检索 + 大模型流式分析，返回结构化结果。
    - emit：可选回调，逐 token 转发生成内容（Web 端 SSE 用）
    """
    retriever = _dimension_retriever(filename)
    docs = retriever.invoke(dim["query"])
    if not docs:
        return {
            "dimension": dim["name"],
            "risk_level": "未入库",
            "evidence": "该合同未入库或未检索到片段",
            "opinion": "请先确认合同已入库（可运行入库功能）",
        }
    context = "\n\n".join(
        f"【片段{i + 1}】{d.page_content}" for i, d in enumerate(docs)
    )
    prompt = _DIM_PROMPT.format(
        dim_name=dim["name"], dim_desc=dim["desc"], context=context
    )
    # 流式生成该维度的审查 JSON（stream=True 时打字机输出 / emit 转发给 Web）
    raw_text = stream_generate(prompt, echo=stream, emit=emit)
    parsed = _parse_json_response(raw_text)
    parsed["dimension"] = dim["name"]
    return parsed


def analyze_contract(name: str, dims=None, progress: bool = True, stream: bool = True) -> list:
    """
    对一份合同执行多维度审查（每维度结果流式输出）。
    - name: 合同文件名（contracts 目录内）或路径
    - dims: 需要审查的维度名列表，默认全部 8 项
    - stream: 是否流式打印生成过程（被 Agent 工具调用时传 False 静默执行）
    返回：结构化审查结果列表。
    """
    file_path = _resolve_file(name)
    filename = os.path.basename(file_path)

    # 审查前确保该合同已入库（幂等去重），使定向检索可用
    add_file_to_kb(file_path)

    if dims:
        dim_list = [d for d in REVIEW_DIMENSIONS if d["name"] in dims]
    else:
        dim_list = REVIEW_DIMENSIONS

    results = []
    for i, dim in enumerate(dim_list, 1):
        if progress:
            print(f"\n  [{i}/{len(dim_list)}] 审查维度：{dim['name']}")
            print("  " + "-" * 40)
        results.append(analyze_dimension(filename, dim, stream=stream))
        if stream:
            print()  # 维度间空行

    # 风险排序：高 > 中 > 低 > 未约定 > 未知
    order = {"高": 0, "中": 1, "低": 2, "未约定": 3}
    results.sort(key=lambda r: order.get(r.get("risk_level"), 9))
    return results


def print_review_report(contract_name: str, results: list) -> None:
    """格式化打印审查报告（答辩演示用）。"""
    print("\n" + "=" * 72)
    print(f"📋 合同审查报告：《{contract_name}》  共 {len(results)} 个维度")
    print("=" * 72)
    for r in results:
        level = r.get("risk_level", "未知")
        icon = {"高": "🔴", "中": "🟠", "低": "🟡", "未约定": "⚪"}.get(level, "❓")
        print(f"\n{icon} 【{r.get('dimension', '')}】风险等级：{level}")
        print(f"   依据原文：{r.get('evidence', '')[:150]}")
        print(f"   审查意见：{r.get('opinion', '')}")
    # 统计
    high = sum(1 for r in results if r.get("risk_level") == "高")
    mid = sum(1 for r in results if r.get("risk_level") == "中")
    print("\n" + "-" * 72)
    print(f"风险汇总：高 {high} 项 / 中 {mid} 项 / 其余 {len(results) - high - mid} 项")
    print("=" * 72)


if __name__ == "__main__":
    # 自检：审查 contracts 目录第一份合同
    first = _list_contract_files()
    if first:
        print(f"开始审查：{first[0]}\n")
        report = analyze_contract(first[0])
        print_review_report(first[0], report)
    else:
        print("contracts 目录下暂无合同文件")
