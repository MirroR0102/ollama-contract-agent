# -*- coding: utf-8 -*-
"""
kb_agent.py —— 知识库问答智能体（轻量模型环节）

为「知识库问答」页提供两个“轻量智能体”环节（均真实调用本地 LLM）：
  1. need_clarify     追问澄清智能体：判断用户问题是否过于模糊/歧义，需要先反问澄清一轮。
  2. verify_evidence  证据校验智能体：对检索到的每条合同片段判定相关性
                      （high 直接相关 / medium 部分相关可参考 / drop 无关或重复），
                      回答只依据保留片段，实现 RAG 的第二道防幻觉闸门。
"""
import json
import re

from agent.contract_kb import stream_generate

_CLARIFY_PROMPT = (
    "你是合同问答的「意图澄清」助手。判断用户问题是否需要先澄清一轮，才能有效检索合同知识库。\n"
    "需要澄清的情况（满足其一）：\n"
    "1. 检索范围为「全部合同」、且问题没有指明具体哪份合同，而不同合同的答案可能不同"
    "（例如只问“违约金怎么约定的/保密期限是多久”，未提合同名）；\n"
    "2. 问题过于宽泛模糊，无法确定检索意图（例如“帮我看看合同”“这份合同有什么问题”）。\n"
    "不需要澄清：问题已足够明确（包含合同名/文件名，或问题本身适用全部合同），不要为问而问。\n"
    "只输出严格 JSON（不要其它文字）：\n"
    "{{\"need\": true或false, \"question\": \"需要澄清时给用户的一句话反问（≤45字，可带选项）；否则为空字符串\"}}\n\n"
    "【当前检索范围】{scope}\n"
    "【用户问题】{question}"
)

_VERIFY_PROMPT = (
    "你是合同问答的「检索证据校验」助手。下面是【用户问题】与检索到的若干【合同片段】，"
    "请逐条判断每个片段对回答该问题的价值：\n"
    "- high：直接相关，回答应主要依据它；\n"
    "- medium：部分相关，可作补充参考；\n"
    "- drop：与问题无关，或与其它 high 片段完全重复（冗余）。\n"
    "规则：宁可多保留（high/medium），只有明显无关或完全重复的片段才标 drop。\n"
    "只输出严格 JSON 数组（不要其它文字）：\n"
    "[{{\"index\": 1, \"relevance\": \"high\", \"reason\": \"一句话理由\"}}, ...]\n\n"
    "【用户问题】{question}\n"
    "【合同片段】\n{docs}"
)


def _extract_json(text: str):
    """从模型输出中容错提取 JSON（去 ```json 包裹 / 取首个 { 或 [ 起的合法子串）。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
    if m:
        t = m.group(1).strip()
    for start_ch, end_ch in (("[", "]"), ("{", "}")):
        s = t.find(start_ch)
        if s < 0:
            continue
        # 从匹配括号向内收缩，直到能解析
        for e in range(len(t), s, -1):
            chunk = t[s:e]
            if chunk.endswith(end_ch):
                try:
                    return json.loads(chunk)
                except Exception:  # noqa: BLE001
                    continue
    return None


def _scope_text(sources) -> str:
    return ("、".join(sources)) if sources else "全部合同"


def need_clarify(question: str, sources=None, model_lock=None) -> dict:
    """追问澄清智能体：判断是否需要反问。返回 {"need": bool, "question": str}。
    模型调用失败或输出无法解析时，保守返回 need=False（不打扰用户，直接检索）。"""
    if not (question or "").strip():
        return {"need": False, "question": ""}
    prompt = _CLARIFY_PROMPT.format(scope=_scope_text(sources), question=question)
    try:
        text = stream_generate(prompt, echo=False)
        data = _extract_json(text)
        if isinstance(data, dict):
            need = bool(data.get("need"))
            q = str(data.get("question") or "").strip()
            return {"need": need, "question": q[:80]}
    except Exception as e:  # noqa: BLE001
        print(f"[kb_agent] 澄清判断失败（按无需澄清处理）：{e}")
    return {"need": False, "question": ""}


def verify_evidence(question: str, docs, model_lock=None) -> list:
    """证据校验智能体：返回与 docs 同序的 [{index, relevance, reason}, ...]。
    relevance ∈ high/medium/drop。模型失败/解析失败时全部按 high 兜底（不丢证据）。"""
    n = len(docs or [])
    fallback = [{"index": i + 1, "relevance": "high", "reason": ""} for i in range(n)]
    if not question or n == 0:
        return fallback
    doc_text = "\n\n".join(
        f"[片段{i + 1}｜来源:{d.metadata.get('source', '未知')}]\n{d.page_content[:500]}"
        for i, d in enumerate(docs))
    prompt = _VERIFY_PROMPT.format(question=question, docs=doc_text)
    try:
        text = stream_generate(prompt, echo=False)
        data = _extract_json(text)
        if isinstance(data, list):
            out = []
            for d in data:
                if not isinstance(d, dict):
                    continue
                idx = int(d.get("index") or 0)
                rel = str(d.get("relevance") or "").lower()
                if rel not in ("high", "medium", "drop"):
                    rel = "high"
                out.append({"index": idx, "relevance": rel,
                            "reason": str(d.get("reason") or "")[:60]})
            if out:
                return out
    except Exception as e:  # noqa: BLE001
        print(f"[kb_agent] 证据校验失败（按全部 high 兜底）：{e}")
    return fallback
