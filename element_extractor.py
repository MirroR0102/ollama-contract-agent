# -*- coding: utf-8 -*-
"""
element_extractor.py —— 合同关键要素结构化抽取
输入一份合同 → 本地 LLM 抽取 {合同类型、当事人、金额、期限、日期、管辖等}，
以 JSON 结构化输出，方便后续二次开发（表单回填、报表、比对）。
是“从非结构化文本到结构化数据”的典型落地场景。
"""
import json
import os
import re

from config import CONTRACTS_DIR, MAX_CONTEXT_CHARS
from contract_analyzer import _resolve_file
from contract_kb import stream_generate
from document_loader import read_plain_text

_EXTRACT_PROMPT = """你是企业合同信息抽取助手。请阅读下方【合同原文】，抽取关键要素。
规则：只依据原文，原文没有的字段填 null，不要猜测。

【合同原文】
{text}

请严格按以下 JSON 格式输出（不要输出任何其他文字）：
{{
  "合同类型": "如：劳动合同/采购合同/保密协议/租赁合同等",
  "甲方": "名称（含统一社会信用代码若出现）",
  "乙方": "名称（含统一社会信用代码若出现）",
  "合同金额": "金额与币种，如 人民币100万元",
  "履行期限": "合同有效期或履行期限",
  "签订日期": "签署日期",
  "生效条件": "生效条件（如签字盖章后生效）",
  "争议解决": "仲裁或诉讼、管辖机构",
  "通知方式": "合同约定的通知送达方式",
  "份数与生效": "合同一式几份、各执几份"
}}
"""


def extract_elements(name: str, stream: bool = True, emit=None) -> dict:
    """
    抽取一份合同的关键要素（流式生成），返回结构化 dict。
    - stream=True：边生成边打印 JSON（打字机效果）；被 Agent 工具调用时传 False 静默。
    - emit：可选回调，逐 token 转发（Web 端 SSE 用）
    """
    file_path = _resolve_file(name)
    text = read_plain_text(file_path)
    # 超长截断，防止超出模型上下文
    if len(text) > MAX_CONTEXT_CHARS:
        print(f"[提示] 合同较长，已截取前 {MAX_CONTEXT_CHARS} 字符进行分析")
        text = text[:MAX_CONTEXT_CHARS]

    prompt = _EXTRACT_PROMPT.format(text=text)
    raw_text = stream_generate(prompt, echo=stream, emit=emit)
    return _parse_json(raw_text)


def _parse_json(text: str) -> dict:
    """容错解析模型输出的 JSON。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        return {"解析失败": str(e), "原始输出": text[:300]}


def print_elements(name: str, data: dict) -> None:
    """格式化打印要素卡片。"""
    print("\n" + "=" * 60)
    print(f"📄 合同要素卡片：《{name}》")
    print("=" * 60)
    if not data:
        print("（未抽取到有效内容）")
        return
    for key, value in data.items():
        print(f"  {key:<8}: {value if value is not None else '—'}")
    print("=" * 60)


if __name__ == "__main__":
    # 自检
    if os.path.isdir(CONTRACTS_DIR):
        files = [f for f in os.listdir(CONTRACTS_DIR) if f.lower().endswith((".txt", ".pdf"))]
        if files:
            data = extract_elements(files[0])
            print_elements(files[0], data)
