# -*- coding: utf-8 -*-
"""
tools.py —— 自定义工具集（供 Agent 自主调用）
使用 @tool 装饰器把系统能力封装为 LLM 可决策调用的工具：
  1. search_contract_knowledge  知识库检索问答（RAG Tool）
  2. analyze_contract           合同多维度智能审查
  3. extract_contract_elements  合同关键要素结构化抽取
  4. list_contract_files        查看库内可用合同清单
"""
import os
import re

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from agent.contract_analyzer import analyze_contract, _list_contract_files
from agent.contract_kb import stream_generate
from agent.element_extractor import extract_elements
from core.config import REVIEW_DIMENSIONS
from core.ollama_conn import retry_emb_call
from store import bootstrap, docparse, session_context
from store.storage import get_store
from store.vector_store import count_vectors, get_retriever


def _thread_ctx(config: RunnableConfig):
    """从工具运行时 config 取 thread_id，查该会话的 (归属用户, 合同范围)。"""
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not thread_id:
        return None, None
    return (session_context.get_owner(thread_id),
            session_context.get_sources(thread_id))


def _thread_id_of(config: RunnableConfig) -> str:
    """从工具运行时 config 取 thread_id（可能为空）。"""
    if not config:
        return ""
    return (config.get("configurable") or {}).get("thread_id") or ""


def _normalize_dims(raw_dims) -> list:
    """把模型传入的 focus_dimensions（可能不精确）规范化为标准维度名列表。"""
    out: list = []
    if not raw_dims:
        return out
    for d in str(raw_dims).split(","):
        d = d.strip().strip("，、;； ")
        if not d:
            continue
        matched = None
        for dim in REVIEW_DIMENSIONS:
            name = dim["name"]
            if name == d or d in name or name in d:
                matched = name
                break
        if matched is None:
            # 别名（query 里的词）匹配
            for dim in REVIEW_DIMENSIONS:
                if any(len(kw) >= 2 and kw in d
                       for kw in re.split(r"[、，,;；]", dim.get("query", ""))):
                    matched = dim["name"]
                    break
        if matched and matched not in out:
            out.append(matched)
    return out


def _guess_dims_from_question(question: str) -> list:
    """按用户问题文本推断其关注的审查维度（未命中返回空=全部维度）。"""
    q = question or ""
    if not q.strip():
        return []
    out: list = []
    for dim in REVIEW_DIMENSIONS:
        name = dim["name"]
        if name in q:
            out.append(name)
            continue
        if any(len(kw) >= 2 and kw in q
               for kw in re.split(r"[、，,;；]", dim.get("query", ""))):
            out.append(name)
    return out


def _owner_files(owner: str) -> list:
    """当前账户合同库的文件名（磁盘唯一名）清单（供“未找到”时的友好提示）。"""
    if not owner:
        return []
    try:
        u = get_store().get_user_by_name(owner)
        return [f.get("store_name") or f["name"]
                for f in get_store().list_files(u["id"])]
    except Exception:  # noqa: BLE001
        return []


def _resolve_agent_path(contract_name: str, owner: str):
    """解析当前会话用户合同库内的文件绝对路径。
    - owner 非空（网页登录场景）：在 uploads/<owner>/ 下解析（找不到返回 None）
    - owner 为空（命令行场景）：原样返回名称，交给 analyze/extract 的 contracts/ 逻辑
    """
    if owner:
        return bootstrap.resolve_user_file(owner, contract_name)
    return contract_name


# ================= 检索兜底：数值/条款类问题的全文关键词扫描 =================
# 「XX多少 / XX是什么 / 违约金 / 押金」等问句仅靠语义向量检索时，长合同里
# 的数字条款（如租金、违约金、期限）可能召回不到，导致模型“没查到就答不出”。
# 这里从问句中提取关键词，若语义检索片段里完全没有这些词，就退化为逐行全文扫描
# （确定性、不依赖嵌入质量），把命中的原文行号与上下文附加给模型。
_QUERY_NUM_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{1,6}?)(?:多少|多少钱|金额|怎么收|收费标准|几|是什么|何种|为何|多久)")
_TERM_HINTS = [
    "违约金", "押金", "保证金", "租金", "物业费", "付款", "保密", "期限", "管辖",
    "争议", "仲裁", "责任", "赔偿", "金额", "费用", "工资", "社保", "试用期",
    "服务费", "利息", "递增", "退还", "解除", "逾期", "税率", "增值税", "发票",
    "折扣", "续租", "转租", "争议解决",
]
_STOP_CORE = re.compile(
    r"(合同|协议|里面|的|中|请问|帮我|看看|一下|这个|那份|所有|我|他|它|内容|都|有什么)")


def _keywords_from_query(query: str) -> list:
    """从问句中提取用于全文扫描的关键词（最多 4 个）。"""
    kws: list = []
    for m in _QUERY_NUM_RE.finditer(query or ""):
        w = (m.group(1) or "").strip()
        if len(w) >= 1 and not _STOP_CORE.search(w) and w not in kws:
            kws.append(w)
    for t in _TERM_HINTS:
        if t in (query or "") and t not in kws:
            kws.append(t)
    return kws[:4]


def _keyword_scan_fallback(query: str, owner, sources: list, max_total: int = 8,
                           ctx_lines: int = 2) -> str:
    """对当前范围内合同逐行扫描 query 关键词，返回原文命中片段（带行号）。"""
    kws = [k.lower() for k in _keywords_from_query(query)]
    if not kws:
        return ""
    rows: list = []
    try:
        if owner:
            u = get_store().get_user_by_name(owner)
            if u:
                rows = get_store().list_contracts(u["id"])
    except Exception:  # noqa: BLE001
        rows = []
    if sources:
        rows = [r for r in rows if (r.get("store_name") or "") in sources]
    results: list = []
    for r in rows:
        store_name = r.get("store_name") or r.get("name") or ""
        if not store_name:
            continue
        try:
            path = bootstrap.resolve_user_file(owner, store_name)
            if not path:
                continue
            parsed = docparse.parse_plain_file(path)
        except Exception:  # noqa: BLE001
            continue
        lines = (parsed.get("text") or "").splitlines()
        for i, ln in enumerate(lines):
            low = ln.lower()
            if any(k in low for k in kws):
                ctx = "\n".join(x.strip()[:150]
                                for x in lines[max(0, i - ctx_lines): i + ctx_lines + 1])
                results.append((r.get("name") or store_name, i + 1, ctx))
                if len(results) >= max_total:
                    break
        if len(results) >= max_total:
            break
    if not results:
        return ""
    out = ["\n[全文关键词扫描补充（逐行命中原文，供核对数值/条款）]"]
    for name, ln, ctx in results:
        out.append(f"\n──《{name}》第 {ln} 行──\n{ctx}")
    return "\n".join(out)


@tool
def search_contract_knowledge(query: str, config: RunnableConfig) -> str:
    """
    检索企业内部合同知识库并返回命中的合同原文片段。
    当用户询问“合同里怎么约定的 / 某条款是什么 / 违约金多少 / 某合同的内容”时必须先调用本工具。
    返回的是与问题最相关的合同原文片段；拿到后请依据片段简洁作答并注明出处。
    注意：检索范围仅限当前会话选定的上下文合同；若该范围内无相关内容，如实告知用户，不要编造。
    Args:
        query: 用户的合同相关问题
    """
    owner, sources = _thread_ctx(config)
    try:
        docs = retry_emb_call(
            lambda: get_retriever(3, sources=sources or None, owner=owner).invoke(query))
    except Exception as e:  # noqa: BLE001
        return f"知识库检索失败：{e}"
    if not docs:
        if sources:
            return ("所选上下文合同范围内未检索到相关内容：请确认所选合同已入库，"
                    "或改为使用「全部合同」后重试。")
        return "知识库未检索到相关合同内容，请先到「合同入库」页导入合同。"
    parts = []
    for i, d in enumerate(docs, 1):
        parts.append(f"[来源：{d.metadata.get('source', '未知')}｜片段{i}]\n{d.page_content}")
    # 兜底：语义检索片段完全不含问题关键词时（长合同数字条款常召回不到），
    # 用全文逐行扫描补上真实原文，避免“模型没证据只能答查无”。
    kws = _keywords_from_query(query)
    if kws:
        hit_in_docs = any(
            any(k in (getattr(d, "page_content", "") or "") for k in kws)
            for d in docs)
        if not hit_in_docs:
            scan = _keyword_scan_fallback(query, owner, sources)
            if scan:
                parts.append(scan)
    return "\n\n".join(parts)

@tool
def analyze_contract_tool(contract_name: str, focus_dimensions: str = "",
                          config: RunnableConfig = None) -> str:
    """
    对当前账户合同库中的指定合同执行多维度智能风险审查，输出风险等级与修改建议。
    当用户要求“审查/分析/检查某合同的风险”时必须调用本工具。
    如不确定合同文件名，请先调用 list_contract_files_tool 获取当前可用合同清单。
    Args:
        contract_name: 当前账户合同库中的合同文件名（如：保密协议_sample.txt，可只写部分名称）
        focus_dimensions: 重点关注的风险维度，逗号分隔，可留空（默认全部维度）
    """
    owner, _sources = _thread_ctx(config)
    # 用统一解析（含下划线/空格/扩展名归一化容错），再取磁盘真实文件名
    row = _resolve_contract_row(owner, contract_name)
    if row is None:
        files = _owner_files(owner)
        hint = "\n".join(f"- {f}" for f in files) if files else "（空）"
        return (f"你的合同库中未找到《{contract_name}》。当前可用合同：\n{hint}\n"
                f"请用准确的合同文件名重试。")
    path = bootstrap.resolve_user_file(owner, row.get("store_name") or row.get("name") or "")
    if not path:
        return f"合同文件在磁盘上不存在（可能已被删除）：《{row.get('name')}》"
    # 维度确定：模型明确给了 focus_dimensions → 规范化；
    # 没给 → 按用户本次问题文本推断（避免问“知识产权风险”却跑全部 8 维）；
    # 仍推不出 → 全部维度（用户未指定方向）
    dims = _normalize_dims(focus_dimensions)
    if not dims:
        thread_id = _thread_id_of(config)
        dims = _guess_dims_from_question(
            session_context.get_question(thread_id) if thread_id else "")
    try:
        # 被 Agent 调用时静默执行（stream=False），结果由 Agent 汇总返回
        results = analyze_contract(path, dims=dims or None, progress=False,
                                   stream=False, owner=owner)
    except Exception as e:  # noqa: BLE001
        return f"审查失败：{e}"
    lines = [f"《{os.path.basename(path)}》审查结果："]
    for r in results:
        lines.append(
            f"- 【{r.get('dimension', '')}】风险:{r.get('risk_level', '未知')} | "
            f"依据:{str(r.get('evidence', ''))[:80]} | 意见:{r.get('opinion', '')}"
        )
    return "\n".join(lines)


@tool
def extract_contract_elements_tool(contract_name: str,
                                   config: RunnableConfig = None) -> str:
    """
    抽取当前账户合同库中合同的关键要素（类型/当事人/金额/期限/日期/争议解决等），
    输出结构化信息。当用户要求“总结/提取某合同的关键信息、当事人、金额、期限”时调用。
    如不确定合同文件名，请先调用 list_contract_files_tool 获取当前可用合同清单。
    Args:
        contract_name: 当前账户合同库中的合同文件名（如：保密协议_sample.txt，可只写部分名称）
    """
    owner, _sources = _thread_ctx(config)
    # 用统一解析（含下划线/空格/扩展名归一化容错），再取磁盘真实文件名
    row = _resolve_contract_row(owner, contract_name)
    if row is None:
        files = _owner_files(owner)
        hint = "\n".join(f"- {f}" for f in files) if files else "（空）"
        return (f"你的合同库中未找到《{contract_name}》。当前可用合同：\n{hint}\n"
                f"请用准确的合同文件名重试。")
    path = bootstrap.resolve_user_file(owner, row.get("store_name") or row.get("name") or "")
    if not path:
        return f"合同文件在磁盘上不存在（可能已被删除）：《{row.get('name')}》"
    try:
        # 被 Agent 调用时静默执行（stream=False）
        data = extract_elements(path, stream=False)
    except Exception as e:  # noqa: BLE001
        return f"抽取失败：{e}"
    lines = [f"《{os.path.basename(path)}》关键要素："]
    for k, v in data.items():
        if v:
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)


@tool
def list_contract_files_tool(config: RunnableConfig) -> str:
    """列出当前可用的合同文件。当用户问“有哪些合同/有什么文件”时调用。"""
    owner, sources = _thread_ctx(config)
    if sources:
        return ("当前上下文合同范围（仅以下文件作为检索依据）：\n"
                + "\n".join(f"- {s}" for s in sources))
    if owner:
        return f"当前账户（{owner}）的合同库文件：\n" + "\n".join(
            f"- {f.get('store_name') or f['name']}" for f in get_store().list_files(
                get_store().get_user_by_name(owner)["id"]))
    files = _list_contract_files()
    if not files:
        return "合同目录为空，尚未导入任何合同。"
    return "当前合同库文件：\n" + "\n".join(f"- {f}" for f in files)


# ================= 工具 5-10：统计 / 元信息 / 文件夹 / 条款定位 / 摘要 / 对比 =================
def _norm_name(s: str) -> str:
    """文件名归一化：去扩展名、去下划线/空格/连字符，用于宽松匹配。"""
    s = re.sub(r"\.(pdf|docx?|txt|md)$", "", (s or "").strip(), flags=re.I)
    return re.sub(r"[\s_\-—]+", "", s).lower()


def _resolve_contract_row(owner: str, name: str):
    """把用户给的名称解析为该用户的合同记录：store_name 精确 → 标题唯一 → 前缀唯一 → 归一化宽松。"""
    if not owner:
        return None
    store = get_store()
    u = store.get_user_by_name(owner)
    if not u:
        return None
    name = (name or "").strip()
    if not name:
        return None
    row = store.get_contract_by_store(u["id"], name)
    if row:
        return row
    rows = store.list_contracts(u["id"])
    hits = [r for r in rows if (r.get("name") or "") == name]
    if len(hits) == 1:
        return hits[0]
    hits = [r for r in rows
            if ((r.get("name") or "").startswith(name)
                or (r.get("store_name") or "").startswith(name))]
    if len(hits) == 1:
        return hits[0]
    # 归一化容错：模型/用户常把文件名里的下划线去掉（如“练习用办公楼宇租赁合同约万字.pdf”
    # 对应磁盘名“练习用_办公楼宇租赁合同_约万字.pdf”），忽略下划线/空格/扩展名后宽松匹配
    nr = _norm_name(name)
    if nr:
        hits = []
        for r in rows:
            rn = _norm_name(r.get("name") or "")
            rs = _norm_name(r.get("store_name") or "")
            if (rn and (rn.startswith(nr) or nr.startswith(rn))) \
                    or (rs and (rs.startswith(nr) or nr.startswith(rs))):
                hits.append(r)
        if len(hits) == 1:
            return hits[0]
    return None


def _contract_hint(owner: str) -> str:
    files = _owner_files(owner)
    return ("\n".join(f"- {f}" for f in files) if files else "（空）")


def _read_contract_text(owner: str, row, max_chars: int = 9000):
    """读取某合同原文全文（txt/pdf/docx/图片OCR），超长截断。返回 (text, err)。"""
    path = bootstrap.resolve_user_file(
        owner, row.get("store_name") or row.get("name") or "")
    if not path:
        return None, "合同文件在磁盘上不存在（可能已被删除）。"
    try:
        r = docparse.parse_plain_file(path)
    except Exception as e:  # noqa: BLE001
        return None, f"文件解析失败：{e}"
    text = (r.get("text") or "").strip()
    if not text:
        return None, f"未能从该文件中提取到文字（类型：{r.get('kind') or '未知'}）。"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（内容较长，已截取前 {max_chars} 字）"
    return text, ""


@tool
def get_kb_stats_tool(config: RunnableConfig) -> str:
    """统计当前上下文范围内合同知识库的规模：合同份数与向量切片总数。
    当用户问“知识库里有多少合同/多少个切片/导入情况”时调用。
    """
    owner, sources = _thread_ctx(config)
    rows: list = []
    if owner:
        u = get_store().get_user_by_name(owner)
        if u:
            rows = get_store().list_contracts(u["id"])
    if sources:
        rows = [r for r in rows if (r.get("store_name") or "") in sources]
    if not rows:
        return (f"当前上下文范围（{len(sources)} 份合同）" if sources
                else "你的合同库") + "中暂无合同记录，请先到「合同入库」页导入合同。"
    total = 0
    detail = []
    for r in rows:
        try:
            n = count_vectors(owner=owner, source=r.get("store_name") or "")
        except Exception:  # noqa: BLE001
            n = 0
        total += n
        detail.append(f"- {r.get('store_name')}：{n} 个切片")
    head = (f"当前范围：仅 {len(rows)} 份合同" if sources else "当前范围：全部合同")
    head += f"，共 {len(rows)} 份合同、{total} 个向量切片。"
    return head + "\n" + "\n".join(detail)


@tool
def get_contract_info_tool(contract_name: str, config: RunnableConfig) -> str:
    """查看某份合同的详细信息：标题/文件名/备注/所属文件夹/大小/入库时间/向量切片数。
    当用户问“XX 合同的备注、放在哪个文件夹、什么时候入库、多大”时调用。
    Args:
        contract_name: 合同标题或文件名（可只写部分名称）
    """
    owner, _s = _thread_ctx(config)
    row = _resolve_contract_row(owner, contract_name)
    if row is None:
        return (f"未找到《{contract_name}》。当前可用合同：\n{_contract_hint(owner)}\n"
                f"请用准确的合同名称重试。")
    try:
        n_vec = count_vectors(owner=owner, source=row.get("store_name") or "")
    except Exception:  # noqa: BLE001
        n_vec = -1
    size_kb = (row.get("size") or 0) / 1024
    return "\n".join([
        f"《{row.get('name')}》",
        f"- 文件名（磁盘）：{row.get('store_name')}",
        f"- 备注：{row.get('note') or '（无）'}",
        f"- 所属文件夹：{row.get('folder_name') or '（默认）'}",
        f"- 大小：{size_kb:.1f} KB",
        f"- 入库时间：{row.get('created_at') or '未知'}",
        f"- 向量切片数：{n_vec if n_vec >= 0 else '读取失败'}",
    ])


@tool
def list_folders_tool(config: RunnableConfig) -> str:
    """按文件夹分组查看合同库结构（每个文件夹及其中的合同）。
    当用户问“合同是怎么分组的/有哪些文件夹/某文件夹里有什么”时调用。
    """
    owner, sources = _thread_ctx(config)
    if not owner:
        return "当前为命令行模式，无文件夹分组信息。"
    u = get_store().get_user_by_name(owner)
    if not u:
        return "用户不存在。"
    folders = get_store().list_folders(u["id"]) or []
    if not folders:
        return "还没有任何合同文件夹。"
    out = [f"你的合同库共 {len(folders)} 个文件夹："]
    for f in folders:
        rows = get_store().list_contracts(u["id"], folder_id=f["id"])
        if sources:
            rows = [r for r in rows if (r.get("store_name") or "") in sources]
        names = [r.get("name") for r in rows]
        out.append(f"\n📁 {f['name']}（{len(names)} 份）：" + (
            "\n   - " + "\n   - ".join(names) if names else "   - （空）"))
    return "\n".join(out)


@tool
def locate_clause_tool(contract_name: str, keyword: str, config: RunnableConfig) -> str:
    """在指定合同原文中定位“包含某关键词的条款/句子”，返回带出处行号的原文片段。
    本工具在全文里逐行精确定位（不调模型、快），适合用户要查看某词（如违约金/保密期限/管辖）
    在某份合同里的原文是怎么写的；与 search_contract_knowledge 语义检索互补。
    Args:
        contract_name: 合同标题或文件名
        keyword: 要定位的关键词，如：违约金、保密期限、管辖
    """
    owner, _s = _thread_ctx(config)
    row = _resolve_contract_row(owner, contract_name)
    if row is None:
        return f"未找到《{contract_name}》。当前可用合同：\n{_contract_hint(owner)}"
    keyword = (keyword or "").strip()
    if not keyword:
        return "请提供要定位的关键词，例如：违约金、保密期限、管辖。"
    text, err = _read_contract_text(owner, row, max_chars=20000)
    if err:
        return err
    lines = text.splitlines()
    low = keyword.lower()
    hits = [i for i, ln in enumerate(lines) if low in ln.lower()]
    if not hits:
        return f"在《{row.get('name')}》全文中未找到包含「{keyword}」的内容。"
    out = [f"《{row.get('name')}》中包含「{keyword}」的原文（共 {len(hits)} 处，仅显示前 6 处）："]
    for i in hits[:6]:
        ctx = "\n".join(lines[max(0, i - 1): i + 2])
        out.append(f"\n──── 第 {i + 1} 行附近 ────\n{ctx.strip()[:400]}")
    return "\n".join(out)


@tool
def summarize_contract_tool(contract_name: str, config: RunnableConfig) -> str:
    """通读整份合同原文，输出结构化中文摘要（类型/当事人/核心内容/风险点）。
    当用户要求“概括/总结一下这份合同、这份合同主要讲什么”时调用。
    Args:
        contract_name: 合同标题或文件名
    """
    owner, _s = _thread_ctx(config)
    row = _resolve_contract_row(owner, contract_name)
    if row is None:
        return f"未找到《{contract_name}》。当前可用合同：\n{_contract_hint(owner)}"
    text, err = _read_contract_text(owner, row, max_chars=16000)
    if err:
        return err
    prompt = (
        "你是合同摘要助手。请基于下方【合同原文】生成结构化中文摘要，严格依据原文、不编造，"
        "按以下格式输出：\n- 合同类型：\n- 签约当事人：\n"
        "- 核心内容（金额、期限、交付/服务内容等关键约定，逐条简短列出）：\n"
        "- 主要风险点或需注意条款：\n\n"
        f"【合同原文】\n{text}"
    )
    try:
        summary = stream_generate(prompt, echo=False)
    except Exception as e:  # noqa: BLE001
        return f"摘要生成失败：{e}"
    return f"《{row.get('name')}》摘要：\n{summary[:2600]}"


@tool
def compare_contracts_tool(contract_a: str, contract_b: str, config: RunnableConfig) -> str:
    """对比两份合同的差异（整体差异 + 金额/期限/违约/保密等关键条款差异）。
    当用户要求“对比/比较两份合同有什么不同/谁更有利”时调用。
    Args:
        contract_a: 第一份合同标题或文件名
        contract_b: 第二份合同标题或文件名
    """
    owner, _s = _thread_ctx(config)
    ra = _resolve_contract_row(owner, contract_a)
    if ra is None:
        return f"未找到《{contract_a}》。当前可用合同：\n{_contract_hint(owner)}"
    rb = _resolve_contract_row(owner, contract_b)
    if rb is None:
        return f"未找到《{contract_b}》。当前可用合同：\n{_contract_hint(owner)}"
    if str(ra.get("id") or "") == str(rb.get("id") or ""):
        return "两份合同是同一份，无需对比。"
    ta, ea = _read_contract_text(owner, ra, max_chars=6000)
    if ea:
        return ea
    tb, eb = _read_contract_text(owner, rb, max_chars=6000)
    if eb:
        return eb
    prompt = (
        "你是合同对比助手。请对比下面两份合同的差异：逐条指出在哪些方面不同"
        "（如当事人、金额、期限、付款、违约责任、保密、争议解决等），并说明各自原文怎么写；"
        "若某方面两份一致可略过。严格依据原文，不编造。\n\n"
        f"【合同一：{ra.get('name')}】\n{ta}\n\n"
        f"【合同二：{rb.get('name')}】\n{tb}"
    )
    try:
        diff = stream_generate(prompt, echo=False)
    except Exception as e:  # noqa: BLE001
        return f"对比生成失败：{e}"
    return f"《{ra.get('name')}》 vs 《{rb.get('name')}》差异：\n{diff[:2600]}"


# Agent 可用的全部工具清单（10 个）
ALL_TOOLS = [
    search_contract_knowledge,      # 1 知识库检索问答
    analyze_contract_tool,          # 2 多维度风险审查
    extract_contract_elements_tool,  # 3 要素抽取
    list_contract_files_tool,       # 4 合同清单
    get_kb_stats_tool,              # 5 知识库统计
    get_contract_info_tool,         # 6 合同详情
    list_folders_tool,              # 7 文件夹分组
    locate_clause_tool,             # 8 条款原文定位
    summarize_contract_tool,        # 9 合同摘要
    compare_contracts_tool,         # 10 合同对比
]
