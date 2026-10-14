# -*- coding: utf-8 -*-
"""
report_export.py —— 按已落库的审查发现导出 docx / pdf。

内容来自 review_findings 和合同标题，不重新调用大模型。
"""
import io
from datetime import datetime

from store.findings import SEVERITY_LABELS, STATUS_LABELS


def _lines(contract: dict, findings: list) -> list:
    title = (contract or {}).get("name") or (contract or {}).get("store_name") or "未命名合同"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "合同审查报告",
        f"合同：{title}",
        f"导出时间：{now}",
        f"条目数：{len(findings or [])}",
        "",
    ]
    if not findings:
        lines.append("本次没有可导出的审查发现。")
        return lines
    for i, row in enumerate(findings, 1):
        sev = SEVERITY_LABELS.get(row.get("severity") or "", row.get("severity") or "")
        st = STATUS_LABELS.get(row.get("status") or "", row.get("status") or "")
        lines.append(f"{i}. {row.get('dimension') or ''}  ·  风险：{sev}  ·  状态：{st}")
        if row.get("title"):
            lines.append(f"标题：{row.get('title')}")
        lines.append("原文摘录：")
        lines.append(row.get("quote") or "（无）")
        lines.append("审查意见：")
        lines.append(row.get("detail") or "（无）")
        if (row.get("note") or "").strip():
            lines.append(f"处理备注：{row.get('note')}")
        lines.append("")
    return lines


def _docx(lines: list) -> bytes:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    for i, line in enumerate(lines):
        p = doc.add_paragraph(line or "")
        if i == 0:
            for run in p.runs:
                run.bold = True
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pdf(lines: list, title: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    head = ParagraphStyle("head", fontName="STSong-Light", fontSize=16, leading=22, spaceAfter=8)
    body = ParagraphStyle("body", fontName="STSong-Light", fontSize=10.5, leading=16, spaceAfter=2)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm, title=title or "合同审查报告",
    )
    flow = []
    for i, line in enumerate(lines):
        esc = (line or " ").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        flow.append(Paragraph(esc, head if i == 0 else body))
        if not line:
            flow.append(Spacer(1, 4))
    doc.build(flow)
    return buf.getvalue()


def export_report(contract: dict, findings: list, fmt: str) -> tuple:
    """返回 (bytes, media_type, ext)。fmt 仅 docx / pdf。"""
    kind = (fmt or "").strip().lower()
    lines = _lines(contract, findings)
    title = (contract or {}).get("name") or "合同审查报告"
    if kind == "docx":
        return _docx(lines), (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ), "docx"
    if kind == "pdf":
        return _pdf(lines, title), "application/pdf", "pdf"
    raise ValueError("format 只能是 docx 或 pdf")
