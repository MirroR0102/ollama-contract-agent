# -*- coding: utf-8 -*-
"""
report_agent.py —— 合同审查报告解读智能体（系统第三个智能体）

输入：合同 8 个风险维度的审查结果（结构化 JSON）
输出：面向业务人员的「风险摘要与行动建议」总结（流式生成）

价值：8 维卡片是"数据"，本智能体把数据归纳成"结论与待办"，
让不懂法务的人也能快速知道：这份合同风险高不高、先处理哪几条。
"""
import json

from agent.contract_kb import stream_generate

_SUMMARY_PROMPT = """你是「合同审查报告解读智能体」。下面是系统对《{contract}》完成的
8 个风险维度审查结果（每条含维度/风险等级/依据原文/审查意见）：

{results}

请以资深企业法务顾问的口吻，为「业务人员（不一定懂法律）」输出一份简洁总结报告，
严格按以下结构（用自然段落 + 短横线列表，不要输出 JSON）：

【总体结论】一句话：这份合同整体风险程度如何，最需要关注的 1~2 个领域是什么。

【优先处理事项】按重要程度排序，列出 3~6 条行动项，每条格式：
- （涉及维度：××）要做/要确认的具体事项，例如"与对方书面确认违约金上限"、"补充保密期限条款"。

【可接受项】若有风险较低、可以接受的维度，用一两句话带过；没有则写"其余维度风险较低，可接受"。

要求：只依据上面给出的审查结果，不要臆造条款；总长度控制在 450 字以内；措辞直接、可执行。"""


def summarize(results: list, contract_name: str = "该合同", emit=None) -> str:
    """对 8 维审查结果生成报告解读（流式）。返回完整文本。

    - results: 审查维度结果列表（dict 列表）
    - contract_name: 合同显示名
    - emit: 可选回调 emit(text 片段)，供 Web 端逐 token 转发
    """
    try:
        payload = json.dumps(results, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        payload = str(results)
    prompt = _SUMMARY_PROMPT.format(contract=contract_name or "该合同", results=payload)
    return stream_generate(prompt, echo=False, emit=emit)
