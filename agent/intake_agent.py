# -*- coding: utf-8 -*-
"""
intake_agent.py —— 文档接入智能体（入库流程编排，系统第二个智能体）

职责：接收一个（或一包）文件 →
  1) 识别真实文件类型（magic bytes）
  2) 自主选择解析方式：txt/pdf/docx 直读、图片 OCR、zip 解压
  3) zip：解压→全部转文字→由模型判断内容构成几份合同（合并/拆分）并给建议标题
  4) 产出「合同候选」列表（标题 / 全文 / 警告）→ 交由入库任务分块入库

模型只参与「需要判断」的环节（zip 分组、标题、质量提示），
解析/OCR 等重活用确定性代码完成——兼顾演示效果与可靠性。
"""
import json
import re

from langchain_core.messages import HumanMessage

from core.ollama_conn import get_llm, retry_llm_call
from store import docparse

# 单文件放入一份“候选合同”的文本上限（超长直接截断，防止入库前上下文爆炸）
MAX_CANDIDATE_CHARS = 60000


# ---------------- 文本 → 分块文档 ----------------
def make_docs(source_name: str, text: str):
    """把整篇文本按合同语义切成 LangChain Document 列表（source=磁盘名）。"""
    from langchain_core.documents import Document
    from store.document_loader import text_splitter
    parts = text_splitter.split_text(text or "")
    return [Document(page_content=p, metadata={"source": source_name})
            for p in parts if p and p.strip()]


# ---------------- zip 智能分组（模型决策） ----------------
_SPLIT_PROMPT = """你是「文档接入智能体」。压缩包解压出的若干文件都已转成文字（图片已 OCR）。
请判断这些内容一共构成几份“合同”，给出分组建议。

规则：
1. 通常每个独立文件 = 一份合同候选；但若多个文件明显是同一份合同的连续部分
   （如 封面+正文、扫描件按页拆成多张图片、被拆开的 PDF），应合并为一份。
2. 单个文件里若明显包含多份不同合同（极少见），可在 groups 中给出多组并说明。
3. 与合同无关或无法判定的文件，放入 ignore_indexes。
4. 每组给一个简洁的合同建议标题（用文件里的甲方/合同名称风格，不要带序号）。

【文件内容摘要】
{members}

请只输出 JSON（不要任何其他文字或代码块）：
{{"groups":[{{"title":"合同建议标题","member_indexes":[文件序号], "reason":"一句话理由"}}],
  "ignore_indexes":[不需要入库的文件序号],
  "warnings":["针对入库的总体提示（无则空数组）"]}}"""


def _parse_json_obj(text: str) -> dict:
    """容错提取模型输出的最外层 JSON 对象。"""
    if not text:
        return {}
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
    if fence:
        t = fence.group(1).strip()
    start = t.find("{")
    if start == -1:
        return {}
    depth, end = 0, -1
    for k in range(start, len(t)):
        if t[k] == "{":
            depth += 1
        elif t[k] == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if end <= start:
        return {}
    try:
        obj = json.loads(t[start:end])
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _llm_split_members(members: list, model_lock=None) -> dict:
    """让模型判断压缩包成员如何构成合同。members 为解析结果列表。
    model_lock：可选的线程锁对象（acquire/release），调模型前获取，防止
    与网页其它大模型推理并发串扰（对应 app.MODEL_LOCK）。
    """
    lines = []
    for i, m in enumerate(members):
        preview = (m.get("text") or "").strip().replace("\n", " ")
        lines.append(f"[文件{i}] 名称:{m.get('name')} 类型:{m.get('kind')} "
                     f"字符数:{len(m.get('text') or '')}\n预览:{preview[:500]}")
    prompt = _SPLIT_PROMPT.format(members="\n".join(lines))
    locked = False
    if model_lock is not None:
        try:
            model_lock.acquire(timeout=600)
            locked = True
        except Exception:  # noqa: BLE001
            locked = False
    try:
        resp = retry_llm_call(
            lambda: get_llm().invoke([HumanMessage(content=prompt)]).content)
        obj = _parse_json_obj(str(resp))
    except Exception:  # noqa: BLE001
        obj = {}
    finally:
        if locked:
            try:
                model_lock.release()
            except Exception:  # noqa: BLE001
                pass
    return obj


def _default_groups(members: list) -> list:
    """模型不可用时兜底：每个文件 = 一份候选（文件名去扩展名作标题）。"""
    groups = []
    for i, m in enumerate(members):
        title = os_splitext_base(m.get("name") or f"合同{i}")
        groups.append({"title": title, "member_indexes": [i], "reason": "默认每文件一份"})
    return groups


def os_splitext_base(name: str) -> str:
    import os
    base = os.path.splitext(os.path.basename(name))[0]
    return base or "合同"


# ---------------- 文件分析入口 ----------------
def analyze_file(file_path: str, emit=None, model_lock=None):
    """分析单个文件（普通 or 压缩包），产出合同候选列表。

    返回 dict：
      {kind, candidates:[{title, text, warnings, reason?}], warnings:[], ignored:[文件名]}
    emit(msg)：可选阶段进度回调。
    model_lock：可选线程锁（zip 分组模型决策前获取）。
    """
    def say(msg):
        if emit:
            emit(msg)

    kind = docparse.sniff_kind(file_path)
    if kind == "zip":
        return _analyze_archive(file_path, say, model_lock)
    return _analyze_plain(file_path, kind, say)


def _analyze_plain(file_path: str, kind: str, say):
    say("接入智能体正在识别文件类型…")
    try:
        r = docparse.parse_plain_file(file_path)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"文件解析失败：{e}")
    text = (r.get("text") or "").strip()
    if not text:
        raise ValueError("未能从文件中提取到任何文本内容。"
                         + ("图片 OCR 可能失败，请尝试更清晰的扫描件。" if kind == "image" else ""))
    if len(text) > MAX_CANDIDATE_CHARS:
        text = text[:MAX_CANDIDATE_CHARS]
        r["warnings"] = r.get("warnings", []) + [f"内容过长，已截取前 {MAX_CANDIDATE_CHARS} 字符入库"]
    say(f"接入智能体已完成解析（{kind}，{len(text)} 字符）")
    return {
        "kind": kind,
        "candidates": [{"title": None, "text": text,
                        "warnings": r.get("warnings", [])}],
        "warnings": list(r.get("warnings", [])),
        "ignored": [],
    }


def _analyze_archive(file_path: str, say, model_lock=None):
    say("接入智能体正在解压压缩包并逐个转换文字…")
    res = docparse.parse_container(file_path)
    parts = res.get("parts", [])
    ok_parts = [p for p in parts if p.get("kind") != "error"]
    err_parts = [p for p in parts if p.get("kind") == "error"]
    if not ok_parts:
        raise ValueError("压缩包内没有可解析的文件。"
                         + (f"（{len(err_parts)} 个文件解析失败）" if err_parts else ""))

    say(f"已转文字 {len(ok_parts)} 份，接入智能体正在判断内容构成几份合同…")
    members = [{"name": p.get("name"), "kind": p.get("kind"),
                "text": p.get("text") or "", "warnings": p.get("warnings", [])}
               for p in ok_parts]
    decision = _llm_split_members(members, model_lock=model_lock)
    groups = decision.get("groups") if isinstance(decision.get("groups"), list) else None
    if not groups:
        groups = _default_groups(members)
    ignore_idx = set(decision.get("ignore_indexes") or [])
    warns = [str(w) for w in (decision.get("warnings") or []) if w]

    candidates = []
    used_idx = set()
    for g in groups:
        idxs = [i for i in (g.get("member_indexes") or [])
                if isinstance(i, int) and 0 <= i < len(members)]
        idxs = [i for i in idxs if i not in ignore_idx]
        if not idxs:
            continue
        used_idx.update(idxs)
        texts, cwarns = [], []
        for i in idxs:
            texts.append(members[i]["text"])
            cwarns += members[i]["warnings"]
        title = str(g.get("title") or "").strip() or os_splitext_base(members[idxs[0]]["name"])
        body = "\n\n".join(texts).strip()
        if not body:
            continue
        if len(body) > MAX_CANDIDATE_CHARS:
            body = body[:MAX_CANDIDATE_CHARS]
            cwarns.append(f"内容过长，已截取前 {MAX_CANDIDATE_CHARS} 字符")
        candidates.append({"title": title, "text": body, "warnings": cwarns,
                           "reason": str(g.get("reason") or "")})

    # 未被任何组收录且未 ignore 的成员，单独补成候选（防漏）
    for i, m in enumerate(members):
        if i in used_idx or i in ignore_idx:
            continue
        body = (m.get("text") or "").strip()
        if body:
            candidates.append({"title": os_splitext_base(m.get("name")),
                               "text": body[:MAX_CANDIDATE_CHARS],
                               "warnings": m.get("warnings", [])})

    ignored = [members[i]["name"] for i in ignore_idx if i < len(members)]
    ignored += [p.get("name") for p in err_parts]
    say(f"接入智能体判断完成：共 {len(candidates)} 份合同候选")
    return {"kind": "zip", "candidates": candidates, "warnings": warns,
            "ignored": ignored}


if __name__ == "__main__":
    import sys
    res = analyze_file(sys.argv[1], emit=print)
    print("\n==== 候选 ====")
    for c in res["candidates"]:
        print(f"- 标题: {c['title']} ｜ {len(c['text'])} 字 ｜ 警告: {c['warnings']}")
    print("忽略:", res.get("ignored"))
