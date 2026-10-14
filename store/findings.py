# -*- coding: utf-8 -*-
"""把审查维度结果整理成 review_findings 的一行。

不改 analyze_dimension 返回给前端的字段名（dimension / risk_level / evidence / opinion）。
一次审查成功后仍只保留最近一次，但已接受 / 已忽略 / 已修改的同类条目会继承状态和备注。
匹配顺序：维度 + 去空白后的原文摘录（截断 80 字，较短的一段被较长的一段包含也算）；
摘录对不上时，再用维度 + 标题。
"""
import json
import re

STATUSES = ("open", "accepted", "dismissed", "fixed")
SEVERITIES = ("high", "medium", "low", "info")

STATUS_LABELS = {
    "open": "未处理",
    "accepted": "已接受",
    "dismissed": "已忽略",
    "fixed": "已修改",
}
SEVERITY_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "info": "提示",
}

_FROM_RISK = {
    "高": "high",
    "中": "medium",
    "低": "low",
    "未约定": "info",
    "未入库": "info",
}


def _clip(text, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def normalize_row(raw: dict) -> dict:
    """写入前规整一行。severity 非法时归为 info。"""
    raw = raw or {}
    dimension = _clip(raw.get("dimension") or "未命名维度", 64) or "未命名维度"
    severity = (raw.get("severity") or "").strip()
    if severity not in SEVERITIES:
        severity = _FROM_RISK.get((raw.get("risk_level") or "").strip(), "info")
    title = _clip(raw.get("title") or "", 200)
    detail = _clip(raw.get("detail") if raw.get("detail") is not None else raw.get("opinion"), 8000)
    quote = _clip(raw.get("quote") if raw.get("quote") is not None else raw.get("evidence"), 8000)
    if not title:
        level = (raw.get("risk_level") or SEVERITY_LABELS.get(severity) or "").strip()
        title = _clip(f"{dimension}（{level or '待复核'}）", 200)
    location = raw.get("location")
    if isinstance(location, (dict, list)):
        location = json.dumps(location, ensure_ascii=False)
    location = _clip(location or "", 2000)
    return {
        "dimension": dimension,
        "severity": severity,
        "title": title,
        "detail": detail,
        "quote": quote,
        "location": location,
    }


_KEEP_STATUS = ("accepted", "dismissed", "fixed")
_RECUR = "复发仍维持原处理"


def _compact(text, limit: int) -> str:
    return re.sub(r"\s+", "", text or "")[:limit]


def merge_prior_decisions(new_rows, old_rows) -> list:
    """新审查覆盖写入前，把已处理结论继承到对得上的新行。"""
    by_quote = {}
    by_title = {}
    for old in old_rows or []:
        if (old.get("status") or "") not in _KEEP_STATUS:
            continue
        qk = ((old.get("dimension") or "").strip(), _compact(old.get("quote"), 80))
        if qk[1]:
            by_quote[qk] = old
        tk = ((old.get("dimension") or "").strip(), _compact(old.get("title"), 40))
        if tk[1]:
            by_title.setdefault(tk, old)
    out = []
    for row in new_rows or []:
        item = dict(row)
        item["status"] = "open"
        item["note"] = ""
        dim = (item.get("dimension") or "").strip()
        quote = _compact(item.get("quote"), 80)
        old = by_quote.get((dim, quote)) if quote else None
        if old is None and quote:
            for (odim, oq), cand in by_quote.items():
                if odim != dim or not oq:
                    continue
                short, long = (oq, quote) if len(oq) <= len(quote) else (quote, oq)
                if len(short) >= 24 and short in long:
                    old = cand
                    break
        if old is None:
            title = _compact(item.get("title"), 40)
            if title:
                old = by_title.get((dim, title))
        if old:
            item["status"] = old.get("status") or "open"
            note = (old.get("note") or "").strip()
            if _RECUR not in note:
                note = (note + " " + _RECUR).strip() if note else _RECUR
            item["note"] = _clip(note, 500)
        out.append(item)
    return out


def rows_from_results(results) -> list:
    """一次审查的维度结果 → findings 行。跳过非 dict。"""
    out = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        out.append(normalize_row({
            "dimension": item.get("dimension"),
            "risk_level": item.get("risk_level"),
            "title": "",
            "detail": item.get("opinion"),
            "quote": item.get("evidence"),
            "location": item.get("location") or "",
        }))
    return out
