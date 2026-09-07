# -*- coding: utf-8 -*-
"""
docparse.py —— 多格式文档解析（txt / pdf / docx / 图片OCR / zip 压缩包 → 文本）

统一入口：parse_any(path)
  * 先按「文件头 magic bytes」探测真实类型（防伪装扩展名）
  * txt/pdf/docx → 直接抽文本
  * jpg/jpeg/png → 本地 OCR（RapidOCR，离线、中文友好）
  * zip → 安全解压到临时目录，逐文件递归解析，汇总为候选文本列表

每个结果带 warnings，前端据此提示用户（如图片 OCR 可能有误）。
"""
import os
import tempfile
import zipfile

# ---------------- 类型探测 ----------------
MAGIC = {
    "pdf": (b"%PDF",),
    "docx_zip": (b"PK",),   # docx 本质是 zip；用扩展名再细分
    "image": (b"\xff\xd8", b"\x89PNG", b"BM"),
    "webp": (b"RIFF",),      # RIFF....WEBP
}
EXT_KIND = {
    ".txt": "txt", ".md": "txt",
    ".pdf": "pdf",
    ".docx": "docx",
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".bmp": "image", ".webp": "image",
    ".zip": "zip",
}


def sniff_kind(path: str) -> str:
    """按扩展名 + 文件头返回真实类型：txt/pdf/docx/image/zip/unknown。"""
    ext = os.path.splitext(path)[1].lower()
    head = b""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except Exception:  # noqa: BLE001
        pass
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        # docx 也是 zip：以扩展名为主，防 zip 伪造成 docx 用 zip 处理
        if ext == ".docx":
            return "docx"
        return "zip"
    if head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG") or head.startswith(b"BM"):
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if ext in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        return "image"
    if ext in (".txt", ".md"):
        return "txt"
    return "unknown"


def is_supported_file(path: str) -> bool:
    return sniff_kind(path) in ("txt", "pdf", "docx", "image", "zip")


# ---------------- 文本类解析 ----------------
def read_txt(path: str) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def read_pdf(path: str) -> str:
    from langchain_community.document_loaders import PyPDFLoader
    try:
        docs = PyPDFLoader(path).load()
        return "\n".join(d.page_content or "" for d in docs)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"PDF 解析失败（可能已加密或损坏）：{e}")


def read_docx(path: str) -> str:
    from docx import Document
    try:
        doc = Document(path)
        parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
        for tb in doc.tables:
            for row in tb.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"docx 解析失败：{e}")


# ---------------- 图片 OCR ----------------
_ocr = None
_ocr_lock = None
_OCR_MIN_CONF = 0.5     # 平均置信度低于此值判定为「可能识别不准」
_OCR_MIN_CHARS = 12     # 识别字数太少也提示


def _get_ocr():
    """惰性单例 RapidOCR（离线，首次初始化较慢）。"""
    global _ocr, _ocr_lock
    if _ocr is None:
        import threading
        from rapidocr_onnxruntime import RapidOCR
        _ocr_lock = threading.Lock()
        with _ocr_lock:
            if _ocr is None:
                _ocr = RapidOCR()
    return _ocr


def ocr_image(path: str):
    """识别图片文字。返回 (text, warnings)。"""
    import numpy as np
    engine = _get_ocr()
    result, _elapse = engine(path)
    lines, confs = [], []
    for item in result or []:
        # item: [box, text, score]
        try:
            txt = str(item[1]).strip()
            score = float(item[2]) if len(item) > 2 and item[2] is not None else 1.0
        except Exception:  # noqa: BLE001
            continue
        if txt:
            lines.append(txt)
            confs.append(score)
    text = "\n".join(lines)
    warnings = []
    if not text.strip():
        warnings.append("未能从图片中识别出文字（可能不是清晰扫描件，或图片不含文字）。")
    else:
        avg = float(np.mean(confs)) if confs else 0.0
        if len(text) < _OCR_MIN_CHARS or avg < _OCR_MIN_CONF:
            warnings.append(
                "图片为扫描件/拍照件，OCR 识别可能存在错误（置信度较低）。"
                "建议入库后用「智能体通读」核对内容是否有误。")
    return text, warnings


# ---------------- zip 解压 ----------------
def _safe_extract(path: str, dest: str) -> list:
    """安全解压 zip 到 dest（防 zip-slip 路径穿越），返回内部文件绝对路径列表。"""
    out = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                continue  # 跳过危险路径
            target = os.path.join(dest, name)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            out.append(target)
    return out


def unpack_zip(path: str):
    """解压 zip 到临时目录，递归收集其中可解析文件。
    返回：(临时目录句柄, [可解析文件绝对路径列表])；调用方负责清理临时目录。
    """
    tmp = tempfile.mkdtemp(prefix="ct_zip_")
    members = _safe_extract(path, tmp)
    usable = []
    for m in members:
        if os.path.isfile(m) and is_supported_file(m):
            usable.append(m)
    return tmp, usable


# ---------------- 统一入口 ----------------
def parse_plain_file(path: str):
    """解析单个普通文件（非压缩包）。返回 {kind, text, warnings}。"""
    kind = sniff_kind(path)
    warnings = []
    if kind == "txt":
        return {"kind": "txt", "text": read_txt(path), "warnings": warnings}
    if kind == "pdf":
        return {"kind": "pdf", "text": read_pdf(path), "warnings": warnings}
    if kind == "docx":
        return {"kind": "docx", "text": read_docx(path), "warnings": warnings}
    if kind == "image":
        text, w = ocr_image(path)
        return {"kind": "image", "text": text, "warnings": warnings + w}
    if kind == "zip":
        raise ValueError("压缩包请使用 parse_container")
    raise ValueError(f"不支持的文件类型：{os.path.basename(path)}")


def parse_container(path: str):
    """解析压缩包：解压 → 逐个解析内部文件。返回 {kind:'zip', parts:[...]}。
    parts: [{name, kind, text, warnings}]（仅包含成功解析出文本者）。
    """
    tmp, usable = unpack_zip(path)
    parts = []
    try:
        for m in usable:
            name = os.path.basename(m)
            try:
                r = parse_plain_file(m)
            except Exception as e:  # noqa: BLE001
                parts.append({"name": name, "kind": "error",
                              "text": "", "warnings": [f"解析失败：{e}"]})
                continue
            r["name"] = name
            parts.append(r)
        return {"kind": "zip", "parts": parts}
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        r = parse_plain_file(p) if sniff_kind(p) != "zip" else parse_container(p)
        print(r["kind"], "→", (r.get("text") or "")[:120].replace("\n", " "),
              "| warnings:", r.get("warnings"))
