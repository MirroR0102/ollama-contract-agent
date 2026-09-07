# -*- coding: utf-8 -*-
"""
draft_export.py —— 合同草案导出（txt / docx / pdf）

把起草智能体输出的纯文本草稿按行还原成可下载文件：
  - txt ：UTF-8 纯文本
  - docx：python-docx，标题行（第X条/章等）加粗
  - pdf ：reportlab + 内置中文字体 STSong-Light（离线可用）
"""
import io
import re

_HEAD_RE = re.compile(
    r"^(第[一二三四五六七八九十百千万\d]+[章节条款]|##+\s*|#{1,3}\s*|\d+\.\s*[^。]{0,30}$)")


def _clean_lines(text: str) -> list:
    return [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    if _HEAD_RE.match(line):
        return True
    # 合同正文常见标题：以「甲方/乙方」开头的头部行或全大写短行不视为标题
    return False


def export_txt(text: str) -> bytes:
    return (text or "").encode("utf-8")


def export_docx(text: str) -> bytes:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    for line in _clean_lines(text):
        p = doc.add_paragraph(line)
        if _is_heading(line):
            for r in p.runs:
                r.bold = True
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_pdf(text: str, title: str = "合同草案") -> bytes:
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    body = ParagraphStyle("body", fontName="STSong-Light", fontSize=10.5,
                          leading=17, alignment=TA_JUSTIFY, spaceAfter=3)
    head = ParagraphStyle("head", parent=body, fontSize=13, leading=20,
                          spaceBefore=8, spaceAfter=5)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=title)
    flow = []
    for line in _clean_lines(text):
        esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        flow.append(Paragraph(esc, head if _is_heading(line) else body))
    doc.build(flow)
    return buf.getvalue()


FORMATS = {"txt": export_txt, "docx": export_docx, "pdf": export_pdf}
MEDIA = {
    "txt": "text/plain; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def export(text: str, fmt: str):
    """按格式导出。返回 (bytes, media_type, ext)。fmt 非法抛 ValueError。"""
    fmt = (fmt or "txt").lower()
    if fmt not in FORMATS:
        raise ValueError(f"不支持的导出格式：{fmt}")
    return FORMATS[fmt](text), MEDIA[fmt], fmt
