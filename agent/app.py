# -*- coding: utf-8 -*-
"""
app.py —— Contract AI 网页版后端（FastAPI + SSE 流式 + 用户系统）
将本地化合同审查系统的全部能力封装为 HTTP 接口：
  知识库问答 / Agent 对话 / 合同审查 / 要素抽取 / 合同入库 / 用户注册登录
全程本地 Ollama 推理，零云依赖。

用户体系：注册/登录后发放 token（Authorization: Bearer <token>，
SSE 的 GET 订阅可用 ?token= 参数）。每个用户的合同库（MySQL/SQLite 归属 +
Chroma owner 隔离）完全独立、互不可见。

启动：python app.py  →  http://localhost:8000
"""
import asyncio
import json
import os
import queue
import re
import threading
import time

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from pydantic import BaseModel

from store import auth, bootstrap, docparse, session_context
from agent.agent_run import _SYSTEM_PROMPT, build_agent
from agent.contract_analyzer import REVIEW_DIMENSIONS, analyze_dimension
from agent.contract_kb import _KB_PROMPT, _format_context, stream_generate
from agent.draft_agent import draft as ai_draft
from agent.draft_export import export as draft_export
from agent.element_extractor import extract_elements
from agent.intake_agent import analyze_file as intake_analyze, make_docs as intake_make_docs
from agent.jobs import (JobAbort, _friendly_error, create_job, get_job,
                        keepalive_all)
from agent.kb_agent import need_clarify as kb_clarify, verify_evidence as kb_verify
from agent.report_agent import summarize as report_summarize
from core.config import LLM_MODEL_NAME, MAX_CONTEXT_CHARS
from core.ollama_conn import (BASE_DELAY, MAX_RETRY, is_conn_error,
                              reset_llm, retry_emb_call)
from store.storage import get_store
from store.vector_store import (add_documents, add_file_to_kb, count_vectors,
                                db_stats, delete_contract_vectors, get_retriever)

# 项目根 = agent/ 的上一级
PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(PROJ_ROOT, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app = FastAPI(title="Contract AI · 本地化智能合同审查系统")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 启动初始化（幂等）：预置演示账号 demo + 演示合同归户入库
bootstrap.init_system()

# Ollama 单模型串行：全局锁避免并发推理错乱
MODEL_LOCK = threading.Lock()

_BUSY_MSG = "系统正忙：正在执行合同审查等后台推理任务，请稍候再试。"


def _try_lock() -> bool:
    """即时请求尝试获取模型锁（拿不到即提示忙，避免无限排队假死）。"""
    return MODEL_LOCK.acquire(timeout=3.0)


# ==================== 鉴权 ====================
def _extract_token(request: Request) -> str:
    """取请求 token：优先 Authorization: Bearer，其次 ?token=（SSE 用）。"""
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.query_params.get("token", "") or request.headers.get("x-token", "")


def current_user(request: Request) -> dict:
    """FastAPI 依赖：解析当前登录用户，无效则 401。"""
    user = auth.user_by_token(_extract_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    return user


@app.middleware("http")
async def _keepalive_middleware(request, call_next):
    """用户仍在发起请求 → 浏览器仍在使用 → 给后台任务续期（避免误中止）。"""
    keepalive_all()
    return await call_next(request)


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """页面与静态资源禁用缓存：改版后刷新即可看到最新版本。"""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path in ("/", "/kb", "/review", "/ingest", "/draft", "/inspect", "/login"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ==================== SSE 基础设施 ====================
def _sse(event: dict) -> str:
    """序列化一个 SSE data 事件。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class _ClientGone(Exception):
    """客户端连接已断开，主动中止当前推理。"""


async def sse_generator(worker):
    """
    通用 SSE 生成器：在后台线程运行 worker(send, stop)，
    worker 通过 send(type, payload) 推送事件，主线程逐条 yield。
    客户端断开时 finally 置 stop 事件，worker 及时中止并释放模型锁。
    """
    q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def send(etype: str, payload=None):
        q.put((etype, payload))

    def run():
        try:
            worker(send, stop)
        except _ClientGone:
            pass  # 客户端已断开，静默退出
        except Exception as e:  # noqa: BLE001
            try:
                q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
            except Exception:
                pass
        finally:
            try:
                q.put(("__end__", None))
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    try:
        while True:
            try:
                etype, payload = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.03)
                continue
            if etype == "__end__":
                break
            yield _sse({"type": etype, "data": payload})
        yield _sse({"type": "done"})
    finally:
        stop.set()


def sse_response(worker) -> StreamingResponse:
    return StreamingResponse(
        sse_generator(worker),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _own_job(job_id: str, user: dict):
    """取属于当前用户的任务；不存在/不属于返回 None。"""
    job = get_job(job_id)
    if not job:
        return None
    if job.owner and job.owner != user["username"]:
        return None
    return job


# ==================== 页面路由 ====================
@app.get("/")
def page_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/kb")
def page_kb():
    return FileResponse(os.path.join(STATIC_DIR, "kb.html"))


@app.get("/review")
def page_review():
    return FileResponse(os.path.join(STATIC_DIR, "review.html"))


@app.get("/ingest")
def page_ingest():
    return FileResponse(os.path.join(STATIC_DIR, "ingest.html"))


@app.get("/draft")
def page_draft():
    return FileResponse(os.path.join(STATIC_DIR, "draft.html"))


@app.get("/inspect")
def page_inspect():
    return FileResponse(os.path.join(STATIC_DIR, "inspect.html"))


@app.get("/login")
def page_login():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


# ==================== 认证接口 ====================
class RegisterBody(BaseModel):
    username: str
    password: str
    display_name: str = ""


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/auth/register")
def api_register(body: RegisterBody):
    """注册新用户（用户名唯一）；成功后自动登录，并把上传根目录遗留文件认领给新用户。"""
    user = auth.register_user(body.username, body.password, body.display_name)
    if not user:
        raise HTTPException(status_code=400, detail="用户名已存在或用户名/密码不合法（至少 1 个字符）")
    sess = auth.login(body.username, body.password)
    try:
        bootstrap.claim_root_orphans(user)
    except Exception as e:  # noqa: BLE001
        print(f"[register] 认领遗留文件失败（可忽略）：{e}")
    return {"ok": True, **sess}


@app.post("/api/auth/login")
def api_login(body: LoginBody):
    sess = auth.login(body.username, body.password)
    if not sess:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"ok": True, **sess}


@app.post("/api/auth/logout")
def api_logout(user: dict = Depends(current_user), request: Request = None):
    auth.logout(_extract_token(request))
    return {"ok": True}


@app.get("/api/me")
def api_me(user: dict = Depends(current_user)):
    return {"username": user["username"], "display_name": user.get("display_name") or user["username"]}


# ==================== 数据接口 ====================
@app.get("/api/files")
def api_files(user: dict = Depends(current_user)):
    """当前用户的合同清单 + 系统状态。
    兼容：每项 name = 磁盘唯一名 store_name（旧前端用它定位/显示），
    新前端用 title/note/folder 等字段展示。
    """
    store = get_store()
    files = []
    for f in store.list_contracts(user["id"]):
        files.append({
            "id": f["id"],
            "name": f.get("store_name") or f["name"],     # 兼容字段：磁盘唯一名
            "title": f["name"],                            # 显示标题（可重名）
            "note": f.get("note") or "",
            "dir": f.get("dir") or "uploads",
            "size": f.get("size") or 0,
            "folder_id": f.get("folder_id"),
            "folder_name": f.get("folder_name") or "",
            "created_at": f.get("created_at"),
        })
    return {
        "username": user["username"],
        "files": files,
        "model": LLM_MODEL_NAME,
        "stats": db_stats(owner=user["username"]),
        "folders": store.list_folders(user["id"]),
    }


# ==================== SSE 流式接口 ====================
class KBQueryBody(BaseModel):
    question: str
    top_k: int = 3  # 检索返回片段数（固定 top 3）
    sources: list[str] = []  # 上下文合同范围（空 = 全部合同）


class ClarifyBody(BaseModel):
    question: str
    sources: list[str] = []  # 上下文合同范围（空 = 全部合同）


@app.post("/api/kb/clarify")
def api_kb_clarify(body: ClarifyBody, user: dict = Depends(current_user)):
    """追问澄清智能体：判断问题是否模糊/歧义、需要先反问澄清一轮。
    返回 {"need": bool, "question": str, "cands": [合同名...]}；
    need=false 时前端直接走检索问答；cands 供前端做“点选合同”的澄清交互。"""
    if not _try_lock():
        raise HTTPException(status_code=429, detail=_BUSY_MSG)
    try:
        store = get_store()
        rows = store.list_contracts(user["id"]) or []
        names = [r.get("store_name") or r.get("name") for r in rows]
        names = [n for n in names if n]
        res = kb_clarify((body.question or "").strip(), body.sources or None,
                         contract_names=names)
        res["cands"] = names
        return res
    finally:
        MODEL_LOCK.release()


@app.post("/api/kb/query")
def api_kb_query(body: KBQueryBody, user: dict = Depends(current_user)):
    """知识库 RAG 问答流水线：检索 → 证据校验智能体 → 依据保留片段作答。
    Ollama 断连自动重建自愈。事件：stage / evidence(带相关性) / token / message。"""

    def worker(send, stop):
        if not _try_lock():
            send("error", {"message": _BUSY_MSG})
            return
        try:
            send("stage", {"text": "🔍 智能体正在检索合同知识库…"})
            # 检索：embedding 连接失效时自动重建实例并重试（lambda 内重建检索器，
            # 确保重试时绑定重建后的新 embedding 实例）
            docs = retry_emb_call(
                lambda: get_retriever(body.top_k, sources=body.sources or None,
                                      owner=user["username"]).invoke(body.question)
            )
            if not docs:
                if body.sources:
                    send("message", {"text": "当前选定的上下文合同范围内未检索到相关内容：请确认所选合同已入库，或改为使用「全部合同」后重试。"})
                else:
                    send("message", {"text": "知识库未检索到相关合同内容，请先到「合同入库」页导入合同。"})
                return
            # 证据校验智能体：逐片段判定相关性，剔除 drop（回答只依据 high/medium）
            send("stage", {"text": "🧐 证据校验智能体正在核验检索片段的相关性…"})
            verdicts = kb_verify(body.question, docs)
            items = []
            kept = []
            for i, d in enumerate(docs):
                v = verdicts[i] if i < len(verdicts) else {"relevance": "high", "reason": ""}
                rel = v.get("relevance", "high")
                items.append({
                    "index": i + 1,
                    "source": d.metadata.get("source", "未知"),
                    "content": d.page_content,
                    "relevance": rel,
                    "reason": v.get("reason", "") or "",
                })
                if rel != "drop":
                    kept.append(d)
            send("evidence", {"items": items})
            if not kept:
                send("message", {"text": "检索到的片段经证据校验均与你的问题不相关：建议换一种问法，或扩大检索范围后重试。"})
                return
            send("stage", {"text": "✍️ 正在依据校验后的证据生成回答…"})
            prompt = _KB_PROMPT.format(context=_format_context(kept), question=body.question)

            def _emit(piece, _stop=stop, _send=send):
                if _stop.is_set():
                    raise _ClientGone()
                _send("token", {"text": piece})

            # 生成：连接失效时 stream_generate 内部自动重建 LLM 连接并重试
            stream_generate(prompt, echo=False, emit=_emit)
        except _ClientGone:
            pass  # 用户手动停止，正常退出（finally 释放模型锁）
        except Exception as e:  # noqa: BLE001
            send("error", {"message": _friendly_error(str(e))})
        finally:
            MODEL_LOCK.release()

    return sse_response(worker)


class ChatBody(BaseModel):
    message: str
    thread_id: str = "web_default"
    sources: list[str] = []  # 上下文合同范围（空 = 全部合同）


# ---------- 要素抽取工具：把工具结果整合为可下载 JSON ----------
_EXTRACT_TOOL = "extract_contract_elements_tool"


def _parse_tool_lines(text: str) -> dict:
    """把要素抽取工具返回的 “- 字段: 值” 行文本还原为 dict；解析不出返回 {}。"""
    data = {}
    if not text:
        return data
    for ln in str(text).splitlines():
        ln = ln.strip()
        if not ln.startswith("- "):
            continue
        body = ln[2:]
        if ":" in body:
            k, v = body.split(":", 1)
            k = k.strip()
            v = v.strip()
            if k and v:
                data[k] = v
    return data


@app.post("/api/chat")
def api_chat(body: ChatBody, user: dict = Depends(current_user)):
    """Agent 多轮对话：登记会话归属与合同范围；Ollama 断连自动重建 Agent 重试。
    流式事件分两类：
      process / tool_call / tool_result / download —— 思考与工具过程（前端小字展示）
      token —— 最终干净回答（前端正文气泡展示）
    """

    def _chat_system_prompt(thread_id: str) -> str:
        """把「当前处理对象」注入系统提示：让每次回答都先声明针对哪份/哪些合同。"""
        owner = session_context.get_owner(thread_id)
        sources = session_context.get_sources(thread_id)
        if not owner:
            desc = "当前为命令行/本地模式，不限定合同范围。"
        elif sources:
            desc = "仅以下合同（用户在页面勾选为当前处理对象）：\n" + \
                "\n".join(f"- {s}" for s in sources)
        else:
            desc = "全部已入库合同（用户未限定单份，即对全部合同库操作）。"
        return _SYSTEM_PROMPT + (
            "\n\n【当前处理对象】" + desc +
            "\n重要说明：以上只是【合同文件的范围清单】，你的上下文中并没有这些合同的原文内容。"
            "\n回答规则（必须遵守）：\n"
            "1) “📌 处理范围：…”声明只能写在【最终文字回答】的第一行；如果你判断需要调用工具，"
            "必须【直接发出工具调用】作为第一条输出——在工具调用之前禁止输出任何文字、"
            "范围声明或“我将调用”类的话术（此类文字无效且不会展示给用户）；\n"
            "2) 只要用户询问任何合同的具体内容（条款、金额、期限、违约金、保密、责任等），"
            "你都必须先调用 search_contract_knowledge 检索真实原文；只有拿到检索结果后才能回答，"
            "检索不到就如实说明未检索到，绝不凭印象编造条款内容；\n"
            "3) 正文中引用条款时注明出自哪份合同文件，且不得编造当前处理对象之外的合同内容。\n"
            "4) 用户要求审查风险时必须真正调用 analyze_contract_tool（contract_name 用当前处理对象"
            "中的文件名），不要只输出“调用/审查中”之类的文字。"
        )

    def worker(send, stop):
        if not _try_lock():
            send("error", {"message": _BUSY_MSG})
            return
        try:
            session_context.set_owner(body.thread_id, user["username"])
            session_context.set_sources(body.thread_id, body.sources or None)
            system_prompt = _chat_system_prompt(body.thread_id)
            # 兜底：最终回答若没自带「处理范围」声明，前端可读到的正文也会以它开头
            _srcs = session_context.get_sources(body.thread_id)
            scope_line = ("📌 处理范围：" + "、".join(_srcs)) if _srcs else "📌 处理范围：全部合同"

            def _run_once():
                """单轮 Agent 流式推送：按「model 消息」整条缓冲后判定——
                消息带工具调用 ⇒ 其中文字是思考草稿（小字过程）；不带工具 ⇒ 最终干净回答。
                （qwen 输出顺序不稳定：可能先文字后工具或先工具后文字，逐 token 判断会误判，
                故以整条 model 消息为单位判定，保证最终回答绝不含草稿。）"""
                emitted = False
                tool_names: dict = {}
                agent = build_agent(system_prompt)
                buf: list = []        # 当前 model 消息的文本缓冲
                msg_tool = False      # 当前 model 消息是否已带工具调用
                prev_node = None

                def finalize_model():
                    """当前 model 消息结束：带工具 → 文字为思考草稿（process）；
                    不带工具 → 文字为最终干净回答（token）。最终回答若漏写处理范围声明则自动补上。"""
                    nonlocal buf, msg_tool
                    if buf:
                        txt = "".join(buf)
                        buf = []
                        if txt:
                            if not msg_tool and "处理范围" not in txt[:24]:
                                txt = scope_line + "\n\n" + txt
                            send("process" if msg_tool else "token", {"text": txt})
                    msg_tool = False

                for chunk, metadata in agent.stream(
                    {"messages": [HumanMessage(content=body.message)]},
                    config={"configurable": {"thread_id": body.thread_id}},
                    stream_mode="messages",
                ):
                    if stop.is_set():
                        return emitted
                    node = (metadata or {}).get("langgraph_node")
                    # 进入 tools 节点 ⇒ 上一条 model 消息结束（必有工具调用）
                    if node == "tools" and prev_node != "tools":
                        finalize_model()
                    prev_node = node
                    tcc = getattr(chunk, "tool_call_chunks", None)
                    if tcc:
                        for tc in tcc:
                            idx = tc.get("index", 0)
                            piece = (tc.get("name") or "").strip()
                            if piece and idx not in tool_names:
                                tool_names[idx] = piece
                                send("tool_call", {"name": piece})
                                emitted = True
                        msg_tool = True
                    text = chunk.content or ""
                    if isinstance(chunk, AIMessageChunk):
                        if text:
                            emitted = True
                            buf.append(text)
                    elif isinstance(chunk, ToolMessage):
                        _raw = str(text)
                        _tname = (getattr(chunk, "name", "") or "")
                        _is_extract = (_tname == _EXTRACT_TOOL
                                       or (_raw.startswith("《") and "关键要素" in _raw[:60]))
                        if (_is_extract
                                and not _raw.startswith(("抽取失败", "你的合同库中未找到"))):
                            _elements = _parse_tool_lines(_raw)
                            if len(_elements) >= 2:
                                # 抽取成功：不把原始键值刷屏，改提示 + 生成可下载 JSON 文件
                                send("tool_result", {
                                    "text": "✓ 关键要素抽取完成，结构化结果已整理为可下载的 JSON 文件（见下方下载卡片）。"})
                                send("download", {
                                    "label": "合同关键要素",
                                    "filename": "合同要素_" + time.strftime("%Y%m%d_%H%M%S") + ".json",
                                    "content": json.dumps(_elements, ensure_ascii=False, indent=2),
                                })
                            else:
                                send("tool_result", {"text": _raw[:200]})
                        else:
                            send("tool_result", {"text": _raw[:200]})
                        emitted = True
                finalize_model()  # 流结束：最后一条 model 消息（闲聊 / 最终回答）
                return emitted

            emitted = False
            for attempt in range(MAX_RETRY + 1):
                try:
                    emitted = _run_once()
                    break  # 整轮成功
                except Exception as e:  # noqa: BLE001
                    if attempt < MAX_RETRY and not emitted and is_conn_error(e):
                        # 连接失效且尚未推送任何内容：重建 LLM 连接后整轮重试
                        time.sleep(BASE_DELAY * (attempt + 1))
                        reset_llm()
                        continue
                    raise
        except _ClientGone:
            pass  # 用户手动停止
        except Exception as e:  # noqa: BLE001
            send("error", {"message": _friendly_error(str(e))})
        finally:
            MODEL_LOCK.release()

    return sse_response(worker)


class DraftBody(BaseModel):
    requirement: str
    reference: str = ""   # 参考蓝本合同文件名（空 = 不参考）
    rewrite: bool = False  # True = 换一种结构与措辞再起草一版


@app.post("/api/draft")
def api_draft(body: DraftBody, user: dict = Depends(current_user)):
    """合同起草智能体：按自然语言需求流式生成中文合同草案。
    可选 reference：读库内某份合同全文作为起草蓝本。"""

    def worker(send, stop):
        if not _try_lock():
            send("error", {"message": _BUSY_MSG})
            return
        try:
            requirement = (body.requirement or "").strip()
            if not requirement:
                send("error", {"message": "请先描述你要起草的合同需求。"})
                return
            # 读取参考蓝本合同全文（作为结构/行文参考）
            ref_text = ""
            if body.reference:
                path = bootstrap.resolve_user_file(user["username"], body.reference)
                if path:
                    try:
                        r = docparse.parse_plain_file(path)
                        ref_text = (r.get("text") or "").strip()[:4000]
                    except Exception as e:  # noqa: BLE001
                        send("error", {"message": f"参考合同读取失败：{e}"})
                        return
                if not ref_text:
                    send("error", {"message": "参考合同未能提取到文本内容。"})
                    return

            def _emit(piece, _stop=stop, _send=send):
                if _stop.is_set():
                    raise _ClientGone()
                _send("token", {"text": piece})

            full = ai_draft(requirement, ref_text=ref_text,
                            mode="rewrite" if body.rewrite else "first", emit=_emit)
            if not (full or "").strip():
                send("error", {"message": "生成结果为空，请稍后重试。"})
        except _ClientGone:
            pass
        except Exception as e:  # noqa: BLE001
            send("error", {"message": _friendly_error(str(e))})
        finally:
            MODEL_LOCK.release()

    return sse_response(worker)


class DraftExportBody(BaseModel):
    text: str = ""
    fmt: str = "txt"   # txt / docx / pdf


@app.post("/api/draft/export")
def api_draft_export(body: DraftExportBody):
    """把草稿文本导出为 txt / docx / pdf 文件下载。"""
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="没有可导出的草稿内容")
    try:
        data, media, ext = draft_export(body.text, body.fmt)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(
        content=data, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="contract_draft.{ext}"'},
    )


class DraftSaveBody(BaseModel):
    text: str = ""
    title: str = ""


@app.post("/api/draft/save")
def api_draft_save(body: DraftSaveBody, user: dict = Depends(current_user)):
    """把 AI 起草的草稿一键存为合同：落盘 uploads/<user>/ + 登记 + 分块向量化。
    入库后即可被知识库问答 / 智能体对话 / 合同审查使用（起草→入库→审查闭环）。"""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="没有可入库的草稿内容")
    title = (body.title or "").strip() or "AI起草合同"
    store = get_store()
    u = store.get_user_by_name(user["username"])
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:40] or "draft"
        store_name = _unique_store_name(user["username"], safe + ".txt")
        dest = os.path.join(bootstrap.user_dir(user["username"]), store_name)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
        rec = store.add_contract(u["id"], title, store_name, "uploads",
                                 len(text.encode("utf-8")), note="AI 起草")
        if not rec:
            raise HTTPException(status_code=409, detail="同名合同已存在，请修改标题后重试")
        docs = intake_make_docs(store_name, text)
        if docs:
            add_documents(docs, owner=user["username"], contract_id=str(rec["id"]))
        return {"ok": True, "store_name": store_name,
                "contract_id": rec["id"], "chunks": len(docs)}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")


class FileBody(BaseModel):
    filename: str
    dims: list[str] = []   # 审查维度子集（空 = 全部 8 维）


# ==================== 后台任务（Job）：跨页面不中断 ====================
def _wait_model_lock(job):
    """后台任务等待模型锁：等待期间若任务应停止则退出返回 False。"""
    while True:
        if MODEL_LOCK.acquire(timeout=1.0):
            return True
        if job.should_stop():
            return False


def _select_dims(names: list) -> list:
    """把用户勾选的维度名映射为 REVIEW_DIMENSIONS 子集；空/非法时回退为全部。"""
    if not names:
        return list(REVIEW_DIMENSIONS)
    valid = [d["name"] for d in REVIEW_DIMENSIONS]
    picked = [n for n in names if n in valid]
    return [d for d in REVIEW_DIMENSIONS if d["name"] in picked] or list(REVIEW_DIMENSIONS)


def run_review_job(job):
    """合同风险审查任务体（限定在任务归属用户的合同库内；可按 job.payload.dims 只审指定维度）。"""
    if not _wait_model_lock(job):
        return
    try:
        path = bootstrap.resolve_user_file(job.owner, job.filename)
        if not path:
            job.status = "error"
            job.error = f"未找到合同文件：{job.filename}"
            return
        try:
            # 确保已入库（幂等）；部分格式（图片/zip 文本）无原生 load，跳过即可
            add_file_to_kb(path, owner=job.owner,
                           contract_id=_contract_id_for(job.owner, path))
        except Exception as e:  # noqa: BLE001
            print(f"[review] 补充入库跳过（不影响检索）：{e}")
        # 预检：该合同的向量库必须存在且非空（避免“查无可查”的静默失败）
        try:
            n_vec = count_vectors(owner=job.owner,
                                  source=os.path.basename(path))
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"合同库读取失败（向量库可能已损坏）：{e}"
            return
        if n_vec <= 0:
            job.status = "error"
            job.error = ("该合同的向量库不存在或为空（可能已损坏或尚未入库）："
                         "请到「合同入库」页重新导入该合同后再审查。")
            return
        cfg = job.payload or {}
        dims = _select_dims(cfg.get("dims") or [])
        job.total = len(dims)
        done_n = len(job.results)  # 断点续跑：跳过已完成的维度
        for i, dim in enumerate(dims, 1):
            if i <= done_n:
                continue
            if job.should_stop():
                return
            job.index, job.dim_name, job.text = i, dim["name"], ""

            def emit(t, _j=job):
                _j.text += t
                if _j.should_stop():
                    raise JobAbort()

            result = analyze_dimension(
                os.path.basename(path), dim, stream=False, emit=emit, owner=job.owner,
            )
            job.results.append(result)
    finally:
        MODEL_LOCK.release()


def run_extract_job(job):
    """合同要素抽取任务体（限定在任务归属用户的合同库内）。"""
    if not _wait_model_lock(job):
        return
    try:
        path = bootstrap.resolve_user_file(job.owner, job.filename)
        if not path:
            job.status = "error"
            job.error = f"未找到合同文件：{job.filename}"
            return
        job.text = ""
        job.dim_name = "关键要素抽取"
        job.total = 1
        job.index = 1

        def emit(t, _j=job):
            _j.text += t
            if _j.should_stop():
                raise JobAbort()

        job.payload = extract_elements(path, stream=False, emit=emit)
    finally:
        MODEL_LOCK.release()


# ==================== 入库流程（v2：暂存 → 智能体分析 → 用户确认 → 入库） ====================
_COPY_SUFFIX = "（副本）"
_NOTE_MAX = 200


def _contract_id_for(username: str, path: str) -> str:
    """按磁盘文件名反查该用户合同记录 id（补充入库时保持向量域一致）。"""
    try:
        store = get_store()
        u = store.get_user_by_name(username)
        if not u:
            return ""
        rec = store.get_contract_by_store(u["id"], os.path.basename(path))
        return str(rec["id"]) if rec else ""
    except Exception:  # noqa: BLE001
        return ""


def _stage_dir(username: str) -> str:
    d = os.path.join(bootstrap.ensure_user_dir(username), ".stage")
    os.makedirs(d, exist_ok=True)
    return d


def _stage_file(username: str, stage_name: str):
    """校验并返回暂存文件绝对路径（不存在返回 None）。含路径穿越防护。"""
    safe = os.path.basename((stage_name or "").replace("\\", "/"))
    if not safe:
        return None
    p = os.path.join(_stage_dir(username), safe)
    return p if os.path.isfile(p) else None


def _unique_store_name(username: str, name: str) -> str:
    """为该用户生成磁盘唯一文件名（DB store_name 与磁盘都不冲突）。"""
    store = get_store()
    u = store.get_user_by_name(username)
    user_dir = bootstrap.user_dir(username)
    base, ext = os.path.splitext(name)
    cand, k = name, 1
    while (store.get_contract_by_store(u["id"], cand)
           or os.path.exists(os.path.join(user_dir, cand))):
        k += 1
        cand = f"{base}_{k}{ext}"
    return cand


def _resolve_folder(store, user, folder_id):
    """校验目标文件夹属于该用户；无效/缺省 → 默认文件夹。返回 folder_id。"""
    if folder_id:
        f = store.get_folder(user["id"], int(folder_id))
        if f:
            return f["id"]
    return store.ensure_default_folder(user["id"])["id"]


def _find_duplicates(store, user, title: str, note: str) -> list:
    """同名标题 且 备注完全相同 的既有合同（重名冲突判定）。"""
    title = (title or "").strip()
    note = note or ""
    if not title:
        return []
    return [d for d in store.find_contracts_by_title(user["id"], title)
            if (d.get("note") or "") == note]


def _ensure_copy_note(note: str) -> str:
    """备注末尾加（副本）；过长先截断，保证“（副本）”一定完整显示。"""
    note = (note or "").rstrip()
    if note.endswith(_COPY_SUFFIX):
        return note
    if len(note) + len(_COPY_SUFFIX) > _NOTE_MAX:
        note = note[: _NOTE_MAX - len(_COPY_SUFFIX)].rstrip()
    return note + _COPY_SUFFIX


def run_ingest_analyze_job(job):
    """文档接入智能体分析任务：识别类型 → 解析/OCR/解压 → 产出合同候选。"""
    path = _stage_file(job.owner, job.filename)
    if not path:
        job.status = "error"
        job.error = "暂存文件不存在或已失效，请重新上传后再分析。"
        return
    job.total = 1
    job.index = 0
    job.set_stage("文档接入智能体已启动：正在识别文件类型…")
    result = intake_analyze(path, emit=job.set_stage, model_lock=MODEL_LOCK)
    job.payload = result
    n = len(result.get("candidates") or [])
    job.set_stage(f"分析完成：识别到 {n} 份合同候选" + ("，请勾选要导入的部分" if n > 1 else ""))


def run_ingest_commit_job(job):
    """按用户确认的 items 入库：登记合同记录 → 落盘 → 分块向量化。"""
    cfg = job.payload or {}
    store = get_store()
    user = store.get_user_by_name(job.owner)
    if not user:
        job.status = "error"
        job.error = "用户不存在"
        return
    src = get_job(str(cfg.get("analyze_job_id") or ""))
    src_payload = (src and src.payload) or None
    if src_payload is None:
        job.status = "error"
        job.error = "分析结果已失效（服务可能已重启），请重新上传并分析后再入库。"
        return
    candidates = src_payload.get("candidates") or []
    items = cfg.get("items") or []
    src_is_zip = src_payload.get("kind") == "zip"
    stage_name = cfg.get("stage_name") or ""
    stage_path = _stage_file(job.owner, stage_name)
    user_dir = bootstrap.user_dir(job.owner)

    # 单文件入库必须依赖暂存原件（首次成功 commit 会将其移走）
    if not src_is_zip and not stage_path:
        job.status = "error"
        job.error = "暂存文件已被使用或不存在，请重新上传后再入库。"
        return

    # zip 候选命名前缀（保留可读性）
    zip_stem = os.path.splitext(stage_name)[0]

    total = len(items)
    job.total = total
    done = 0
    for i, it in enumerate(items, 1):
        if job.should_stop():
            return
        job.index = i
        idx = int(it.get("index") or 0)
        cand = candidates[idx] if 0 <= idx < len(candidates) else None
        if cand is None:
            job.set_stage(f"跳过第 {i}/{total} 项：候选数据缺失")
            continue
        title = (it.get("title") or "").strip() or (cand.get("title") or f"合同{i}")
        note = (it.get("note") or "").strip()
        folder_id = _resolve_folder(store, user, it.get("folder_id"))
        text = cand.get("text") or ""

        job.set_stage(f"[{i}/{total}] 正在为《{title}》创建合同记录…")
        if src_is_zip:
            # zip 拆出的候选：落一份文本文件（供后续审查/抽取），命名可读唯一
            store_name = _unique_store_name(job.owner, f"{zip_stem}__P{idx}.txt")
            dest = os.path.join(user_dir, store_name)
            try:
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:  # noqa: BLE001
                job.set_stage(f"写入《{title}》文本失败：{e}")
                continue
            size = len(text.encode("utf-8"))
        else:
            # 单文件：把暂存原件移动到用户目录（保留原始格式）
            store_name = _unique_store_name(job.owner, stage_name)
            dest = os.path.join(user_dir, store_name)
            if stage_path and os.path.exists(stage_path):
                try:
                    size = os.path.getsize(stage_path)
                    os.replace(stage_path, dest)
                except Exception as e:  # noqa: BLE001
                    job.set_stage(f"保存《{title}》失败：{e}")
                    continue
            else:
                job.set_stage(f"《{title}》暂存文件缺失，跳过")
                continue

        rec = store.add_contract(user["id"], title, store_name, "uploads",
                                 size, folder_id=folder_id, note=note)
        if not rec:
            job.set_stage(f"《{title}》登记失败（可能重名冲突），已跳过")
            continue
        # 分块 + 向量化（该合同独立向量域 contract_id）
        job.set_stage(f"[{i}/{total}] 正在分块向量化《{title}》…")
        docs = intake_make_docs(store_name, text)
        if docs:
            add_documents(docs, owner=job.owner, contract_id=str(rec["id"]))
        done += 1
        job.set_stage(f"[{i}/{total}] 《{title}》入库完成（{len(docs)} 个切片）")

    # 清理暂存（zip 原件等；单文件已被 move 走）
    if stage_path and os.path.exists(stage_path):
        try:
            os.remove(stage_path)
        except Exception:  # noqa: BLE001
            pass
    job.set_stage(f"入库完成：共 {done}/{total} 份合同已入库")
    job.payload = {"ok": done}


class SummaryBody(BaseModel):
    filename: str = ""
    results: list = []     # 审查维度结果列表


def run_report_summary_job(job):
    """报告解读智能体任务（后台运行，切页不打断）：对 8 维结果流式归纳总结。"""
    if not _wait_model_lock(job):
        return
    try:
        cfg = job.payload or {}
        results = cfg.get("results") or []
        filename = cfg.get("filename") or "该合同"
        job.set_stage("报告解读智能体正在生成总结…")

        def emit(t, _j=job):
            _j.text += t
            if _j.should_stop():
                raise JobAbort()

        full = report_summarize(results, filename, emit=emit)
        job.payload = full  # 存全文，供刷新/切页回来直接恢复
    finally:
        MODEL_LOCK.release()


@app.post("/api/review/summary")
def api_review_summary(body: SummaryBody, user: dict = Depends(current_user)):
    """提交「报告解读」后台任务：立即返回 job_id（SSE 订阅 /api/jobs/{id}/stream）。
    后台任务不依赖页面存活：切页/刷新都不打断生成。"""
    results = [r for r in (body.results or []) if isinstance(r, dict)]
    if not results:
        raise HTTPException(status_code=400, detail="没有可解读的审查结果")
    job = create_job(
        "report_summary", (body.filename or "summary")[:120], run_report_summary_job,
        owner=user["username"],
        payload={"filename": body.filename or "该合同", "results": results})
    return {"ok": True, "job_id": job.id}


@app.post("/api/analyze")
def api_analyze(body: FileBody, user: dict = Depends(current_user)):
    """提交合同审查任务（可按 dims 只审指定维度）：立即返回 job_id。"""
    if not bootstrap.resolve_user_file(user["username"], body.filename):
        raise HTTPException(status_code=404, detail=f"你的合同库中不存在文件：{body.filename}")
    dims = _select_dims(body.dims or [])
    job = create_job("review", body.filename, run_review_job, owner=user["username"],
                     payload={"dims": [d["name"] for d in dims]})
    return {
        "ok": True,
        "job_id": job.id,
        "filename": body.filename,
        "total_dims": len(dims),
    }


@app.post("/api/extract")
def api_extract(body: FileBody, user: dict = Depends(current_user)):
    """提交合同要素抽取任务：立即返回 job_id。"""
    if not bootstrap.resolve_user_file(user["username"], body.filename):
        raise HTTPException(status_code=404, detail=f"你的合同库中不存在文件：{body.filename}")
    job = create_job("extract", body.filename, run_extract_job, owner=user["username"])
    return {"ok": True, "job_id": job.id, "filename": body.filename}


_INSPECT_FIELDS = ["合同类型", "甲方", "乙方", "合同金额", "履行期限",
                  "签订日期", "生效条件", "争议解决", "通知方式", "份数与生效"]


def run_inspect_job(job):
    """合同巡检任务：批量抽取该用户全部合同的要素，汇成可比对的表格行。"""
    store = get_store()
    u = store.get_user_by_name(job.owner)
    if not u:
        job.status = "error"
        job.error = "用户不存在"
        return
    rows = store.list_contracts(u["id"]) or []
    if not rows:
        job.status = "error"
        job.error = "合同库为空，请先到「合同入库」页导入合同后再巡检。"
        return
    if not _wait_model_lock(job):
        return
    try:
        total = len(rows)
        job.total = total
        out_rows: list = []
        for idx, rec in enumerate(rows, 1):
            if job.should_stop():
                return
            name = rec.get("store_name") or rec.get("name") or ""
            title = rec.get("name") or name
            job.index = idx
            job.dim_name = name
            job.set_stage(f"[{idx}/{total}] 正在抽取《{title}》的关键要素…")
            data: dict = {}
            path = bootstrap.resolve_user_file(job.owner, name)
            if path:
                try:
                    r = docparse.parse_plain_file(path)
                    txt = (r.get("text") or "")
                    if len(txt) > MAX_CONTEXT_CHARS:
                        txt = txt[:MAX_CONTEXT_CHARS]
                    if txt.strip():
                        data = extract_elements(name, stream=False, text=txt)
                except Exception as e:  # noqa: BLE001
                    data = {}
            row = {"file": name, "title": title,
                   "folder": rec.get("folder_name") or ""}
            if isinstance(data, dict):
                for k in _INSPECT_FIELDS:
                    row[k] = str(data.get(k) or "").strip()
            if not isinstance(data, dict) or not any(row.get(k) for k in _INSPECT_FIELDS):
                row["合同类型"] = "(抽取失败)"
            out_rows.append(row)
            job.payload = {"rows": list(out_rows)}  # 增量保存，中断也可查看部分结果
        job.set_stage(f"巡检完成：共抽取 {len(out_rows)} 份合同")
        job.payload = {"rows": out_rows}
    finally:
        MODEL_LOCK.release()


@app.post("/api/inspect/run")
def api_inspect_run(user: dict = Depends(current_user)):
    """提交「合同巡检」后台任务：批量抽取全部合同要素。"""
    job = create_job("inspect", "合同巡检", run_inspect_job, owner=user["username"])
    return {"ok": True, "job_id": job.id, "job_kind": "inspect"}


def _own_or_404(job_id: str, user: dict):
    job = _own_job(job_id, user)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或无权访问")
    return job


@app.get("/api/jobs/{job_id}/status")
def api_job_status(job_id: str, user: dict = Depends(current_user)):
    """查询任务状态（页面刷新 / 重新打开时恢复进度用）。不存在/无权访问返回 exists=false。"""
    job = get_job(job_id)
    if not job:
        return {"exists": False}
    if job.owner and job.owner != user["username"]:
        return {"exists": False}
    return {"exists": True, **job.snapshot()}


@app.post("/api/jobs/{job_id}/leave")
def api_job_leave(job_id: str, user: dict = Depends(current_user)):
    """页面离开时上报（sendBeacon）：后台由此重算「离开超时」。"""
    job = _own_or_404(job_id, user)
    job.touch()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/abort")
def api_job_abort(job_id: str, user: dict = Depends(current_user)):
    """彻底终止后台任务（不可恢复）。"""
    _own_or_404(job_id, user).abort("已手动终止")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pause")
def api_job_pause(job_id: str, user: dict = Depends(current_user)):
    """暂停任务（保留进度，可继续）。"""
    _own_or_404(job_id, user).pause()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def api_job_resume(job_id: str, user: dict = Depends(current_user)):
    """从暂停处继续任务。"""
    _own_or_404(job_id, user).resume()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str, user: dict = Depends(current_user)):
    """订阅任务进度（SSE）。支持多端同时订阅；单个页面断开不影响任务继续。"""
    job = _own_or_404(job_id, user)

    async def gen():
        job.attach()
        last = {
            "index": job.index,
            "textlen": len(job.text or ""),
            "nres": len(job.results),
            "sver": job._stage_ver,
            "status": job.status,
        }
        try:
            yield _sse({"type": "snapshot", "data": job.snapshot()})
            last_send = time.time()
            while True:
                evs = job.events_since(last)
                now = time.time()
                if evs or (now - last_send) >= 10:
                    for etype, payload in evs:
                        yield _sse({"type": etype, "data": payload})
                    if not evs:
                        yield ": ping\n\n"  # 心跳，防止长空闲断连
                    last_send = now
                if job.status != "running":
                    if job.status == "done":
                        yield _sse({"type": "done"})
                    elif job.status == "paused":
                        yield _sse({"type": "paused", "data": {"results": len(job.results)}})
                    else:
                        yield _sse({"type": "error", "data": {"message": job.error or "任务已中止"}})
                    break
                await asyncio.sleep(0.2)
        finally:
            job.detach()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================== 文件夹 / 合同管理 ====================
def _delete_contract_assets(user, contract) -> None:
    """删除合同：向量切片 + 磁盘文件 + 数据库行。"""
    try:
        delete_contract_vectors(user["username"], contract["id"])
    except Exception as e:  # noqa: BLE001
        print(f"[删除] 向量清理失败（可忽略）：{e}")
    store_name = contract.get("store_name")
    if store_name:
        p = os.path.join(bootstrap.user_dir(user["username"]), store_name)
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:  # noqa: BLE001
            pass
    get_store().delete_contract(user["id"], contract["id"])


@app.get("/api/folders")
def api_folders(user: dict = Depends(current_user)):
    """当前用户的合同库文件夹清单（含各自合同数）。"""
    store = get_store()
    return {"folders": store.list_folders(user["id"])}


class FolderBody(BaseModel):
    name: str


@app.post("/api/folders")
def api_folder_create(body: FolderBody, user: dict = Depends(current_user)):
    """新建合同库文件夹。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件夹名称不能为空")
    if len(name) > 50:
        raise HTTPException(status_code=400, detail="文件夹名称过长（最多 50 字）")
    f = get_store().create_folder(user["id"], name)
    if not f:
        raise HTTPException(status_code=400, detail="已存在同名文件夹")
    return {"ok": True, "folder": f}


@app.delete("/api/folders/{folder_id}")
def api_folder_delete(folder_id: int, user: dict = Depends(current_user)):
    """删除文件夹及其下全部合同（前端需两次确认）。"""
    store = get_store()
    folder = store.get_folder(user["id"], folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="文件夹不存在或无权访问")
    contracts = store.list_contracts(user["id"], folder_id=folder_id)
    for c in contracts:
        _delete_contract_assets(user, c)
    store.delete_folder_row(user["id"], folder_id)
    return {"ok": True, "deleted_contracts": len(contracts)}


@app.delete("/api/contracts/{contract_id}")
def api_contract_delete(contract_id: int, user: dict = Depends(current_user)):
    """删除单份合同（含向量切片与文件；前端需两次确认）。"""
    store = get_store()
    contract = store.get_contract(user["id"], contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="合同不存在或无权访问")
    _delete_contract_assets(user, contract)
    return {"ok": True}


class ContractUpdateBody(BaseModel):
    name: str = None        # 显示标题
    note: str = None        # 备注
    folder_id: int = None   # 移入文件夹


@app.patch("/api/contracts/{contract_id}")
def api_contract_update(contract_id: int, body: ContractUpdateBody,
                        user: dict = Depends(current_user)):
    """更新合同：改标题 / 备注 / 移动到其它文件夹。"""
    store = get_store()
    contract = store.get_contract(user["id"], contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="合同不存在或无权访问")
    name = None
    if body.name is not None:
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="合同名称不能为空")
    folder_id = None
    if body.folder_id is not None:
        f = store.get_folder(user["id"], int(body.folder_id))
        if not f:
            raise HTTPException(status_code=404, detail="目标文件夹不存在或无权访问")
        folder_id = f["id"]
    ok = store.update_contract(user["id"], contract_id, name=name,
                               note=body.note if body.note is not None else None,
                               folder_id=folder_id)
    return {"ok": bool(ok)}


# ==================== 入库（v2：多格式 + 接入智能体 + 两段式） ====================
_ALLOW_EXTS = {".txt", ".pdf", ".docx", ".jpg", ".jpeg", ".png", ".zip"}
_MAX_UPLOAD = 80 * 1024 * 1024  # 80MB


class StageNameBody(BaseModel):
    stage_name: str


class CheckBody(BaseModel):
    items: list = []          # [{index, title, note}]


class CommitBody(BaseModel):
    analyze_job_id: str = ""
    stage_name: str = ""
    items: list = []          # [{index,title,note,folder_id,append_copy}]


@app.post("/api/ingest/stage")
async def api_ingest_stage(file: UploadFile = File(...),
                           user: dict = Depends(current_user)):
    """第一步：上传文件到暂存区（不做任何解析/入库）。"""
    name = (file.filename or "contract.txt").replace("\\", "/")
    safe = os.path.basename(name).replace("/", "_")
    ext = os.path.splitext(safe)[1].lower()
    if ext not in _ALLOW_EXTS:
        raise HTTPException(
            status_code=400,
            detail="暂不支持该格式。支持：txt / pdf / docx / 图片(jpg,jpeg,png) / zip 压缩包")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(content) > _MAX_UPLOAD:
        raise HTTPException(status_code=400, detail="文件过大（上限 80MB）")
    stage_path = os.path.join(_stage_dir(user["username"]), safe)
    # 重传同名暂存直接覆盖（旧暂存未完成即丢弃）
    if os.path.exists(stage_path):
        try:
            os.remove(stage_path)
        except Exception:  # noqa: BLE001
            pass
    with open(stage_path, "wb") as f:
        f.write(content)
    return {"ok": True, "stage_name": safe, "size": len(content), "ext": ext}


@app.post("/api/ingest/discard")
def api_ingest_discard(body: StageNameBody, user: dict = Depends(current_user)):
    """放弃暂存文件。"""
    p = _stage_file(user["username"], body.stage_name)
    if p:
        try:
            os.remove(p)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


@app.post("/api/ingest/analyze")
def api_ingest_analyze(body: StageNameBody, user: dict = Depends(current_user)):
    """第二步：后台「文档接入智能体」分析暂存文件（识别类型/解析/切分）。"""
    p = _stage_file(user["username"], body.stage_name)
    if not p:
        raise HTTPException(status_code=404, detail="暂存文件不存在，请重新上传")
    job = create_job("ingest_analyze", body.stage_name, run_ingest_analyze_job,
                     owner=user["username"])
    return {"ok": True, "job_id": job.id}


@app.post("/api/ingest/check")
def api_ingest_check(body: CheckBody, user: dict = Depends(current_user)):
    """同步预检：哪些项存在「同名标题 + 备注完全相同」的既有合同。"""
    store = get_store()
    conflicts = []
    for it in (body.items or []):
        title = (it.get("title") or "").strip()
        note = it.get("note") or ""
        dups = _find_duplicates(store, user, title, note)
        if dups:
            conflicts.append({
                "index": it.get("index", 0),
                "title": title,
                "note": note,
                "exists": [{"title": d.get("name"), "note": d.get("note") or ""}
                           for d in dups[:3]],
            })
    return {"conflicts": conflicts}


@app.post("/api/ingest/commit")
def api_ingest_commit(body: CommitBody, user: dict = Depends(current_user)):
    """第三步：按用户确认的候选清单入库（登记 → 落盘 → 向量化，后台任务）。"""
    store = get_store()
    src = get_job(body.analyze_job_id or "")
    if not src or src.owner != user["username"] or src.payload is None:
        raise HTTPException(status_code=400, detail="分析结果已失效，请重新上传并分析")
    items = list(body.items or [])
    if not items:
        raise HTTPException(status_code=400, detail="未选择任何要入库的合同")

    # 冲突预检：append_copy=false 且存在同名同备注 → 409 交由前端确认
    conflicts = []
    for it in items:
        if it.get("append_copy"):
            continue
        title = (it.get("title") or "").strip()
        note = it.get("note") or ""
        if title and _find_duplicates(store, user, title, note):
            conflicts.append({"index": it.get("index", 0), "title": title, "note": note})
    if conflicts:
        raise HTTPException(status_code=409,
                            detail={"conflicts": conflicts,
                                    "message": "存在同名且备注完全相同的合同"})

    # 确认副本的项：后端统一在备注末尾加（副本）（截断保证可见）
    final_items = []
    for it in items:
        item = dict(it)
        if item.get("append_copy"):
            title = (item.get("title") or "").strip()
            note = item.get("note") or ""
            if title and _find_duplicates(store, user, title, note):
                item["note"] = _ensure_copy_note(note)
        final_items.append(item)

    payload = {"analyze_job_id": body.analyze_job_id,
               "stage_name": body.stage_name, "items": final_items}
    job = create_job("ingest_commit", body.stage_name, run_ingest_commit_job,
                     owner=user["username"], payload=payload)
    return {"ok": True, "job_id": job.id, "total": len(final_items)}


if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("  Contract AI · 本地化智能合同审查系统（网页版 · 用户版）")
    print(f"  模型：{LLM_MODEL_NAME}   服务：http://localhost:8000")
    print(f"  预置演示账号：demo / demo123（含保密协议等演示合同）")
    print("  Ctrl+C 退出")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
