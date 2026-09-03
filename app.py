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
import threading
import time

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from pydantic import BaseModel

import auth
import bootstrap
import session_context
from agent_run import kb_agent
from config import LLM_MODEL_NAME
from contract_analyzer import REVIEW_DIMENSIONS, analyze_dimension
from contract_kb import _KB_PROMPT, _format_context, llm
from element_extractor import extract_elements
from jobs import JobAbort, create_job, get_job, keepalive_all
from storage import get_store
from vector_store import add_file_to_kb, db_stats, get_retriever

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
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
    if path.startswith("/static/") or path in ("/", "/kb", "/review", "/ingest", "/login"):
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
    """当前用户的合同清单 + 系统状态。"""
    store = get_store()
    return {
        "username": user["username"],
        "files": store.list_files(user["id"]),
        "model": LLM_MODEL_NAME,
        "stats": db_stats(owner=user["username"]),
    }


# ==================== SSE 流式接口 ====================
class KBQueryBody(BaseModel):
    question: str
    top_k: int = 3  # 检索返回片段数（固定 top 3）
    sources: list[str] = []  # 上下文合同范围（空 = 全部合同）


@app.post("/api/kb/query")
def api_kb_query(body: KBQueryBody, user: dict = Depends(current_user)):
    """知识库 RAG 问答（仅检索当前用户合同库）。"""

    def worker(send, stop):
        if not _try_lock():
            send("error", {"message": _BUSY_MSG})
            return
        try:
            retriever = get_retriever(body.top_k, sources=body.sources or None,
                                      owner=user["username"])
            docs = retriever.invoke(body.question)
            if not docs:
                if body.sources:
                    send("message", {"text": "当前选定的上下文合同范围内未检索到相关内容：请确认所选合同已入库，或改为使用「全部合同」后重试。"})
                else:
                    send("message", {"text": "知识库未检索到相关合同内容，请先到「合同入库」页导入合同。"})
                return
            send("evidence", {
                "items": [
                    {"index": i + 1,
                     "source": d.metadata.get("source", "未知"),
                     "content": d.page_content}
                    for i, d in enumerate(docs)
                ]
            })
            prompt = _KB_PROMPT.format(context=_format_context(docs), question=body.question)
            for chunk in llm.stream([HumanMessage(content=prompt)]):
                if stop.is_set():
                    return
                piece = chunk.content or ""
                if piece:
                    send("token", {"text": piece})
        finally:
            MODEL_LOCK.release()

    return sse_response(worker)


class ChatBody(BaseModel):
    message: str
    thread_id: str = "web_default"
    sources: list[str] = []  # 上下文合同范围（空 = 全部合同）


@app.post("/api/chat")
def api_chat(body: ChatBody, user: dict = Depends(current_user)):
    """Agent 多轮对话：登记会话的归属用户与合同范围，检索工具据此隔离。"""

    def worker(send, stop):
        if not _try_lock():
            send("error", {"message": _BUSY_MSG})
            return
        try:
            session_context.set_owner(body.thread_id, user["username"])
            session_context.set_sources(body.thread_id, body.sources or None)
            tool_names: dict = {}
            for chunk, metadata in kb_agent.stream(
                {"messages": [HumanMessage(content=body.message)]},
                config={"configurable": {"thread_id": body.thread_id}},
                stream_mode="messages",
            ):
                if stop.is_set():
                    return
                tcc = getattr(chunk, "tool_call_chunks", None)
                if tcc:
                    for tc in tcc:
                        idx = tc.get("index", 0)
                        piece = (tc.get("name") or "").strip()
                        if piece and idx not in tool_names:
                            tool_names[idx] = piece
                            send("tool_call", {"name": piece})
                text = chunk.content or ""
                if isinstance(chunk, AIMessageChunk):
                    if text:
                        send("token", {"text": text})
                elif isinstance(chunk, ToolMessage):
                    send("tool_result", {"text": str(text)[:200]})
        finally:
            MODEL_LOCK.release()

    return sse_response(worker)


class FileBody(BaseModel):
    filename: str


# ==================== 后台任务（Job）：跨页面不中断 ====================
def _wait_model_lock(job):
    """后台任务等待模型锁：等待期间若任务应停止则退出返回 False。"""
    while True:
        if MODEL_LOCK.acquire(timeout=1.0):
            return True
        if job.should_stop():
            return False


def run_review_job(job):
    """合同 8 维度审查任务体（限定在任务归属用户的合同库内）。"""
    if not _wait_model_lock(job):
        return
    try:
        path = bootstrap.resolve_user_file(job.owner, job.filename)
        if not path:
            job.status = "error"
            job.error = f"未找到合同文件：{job.filename}"
            return
        add_file_to_kb(path, owner=job.owner)  # 确保已入库（幂等）
        job.total = len(REVIEW_DIMENSIONS)
        done_n = len(job.results)  # 断点续跑：跳过已完成的维度
        for i, dim in enumerate(REVIEW_DIMENSIONS, 1):
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


@app.post("/api/analyze")
def api_analyze(body: FileBody, user: dict = Depends(current_user)):
    """提交合同 8 维度审查任务：立即返回 job_id。"""
    if not bootstrap.resolve_user_file(user["username"], body.filename):
        raise HTTPException(status_code=404, detail=f"你的合同库中不存在文件：{body.filename}")
    job = create_job("review", body.filename, run_review_job, owner=user["username"])
    return {
        "ok": True,
        "job_id": job.id,
        "filename": body.filename,
        "total_dims": len(REVIEW_DIMENSIONS),
    }


@app.post("/api/extract")
def api_extract(body: FileBody, user: dict = Depends(current_user)):
    """提交合同要素抽取任务：立即返回 job_id。"""
    if not bootstrap.resolve_user_file(user["username"], body.filename):
        raise HTTPException(status_code=404, detail=f"你的合同库中不存在文件：{body.filename}")
    job = create_job("extract", body.filename, run_extract_job, owner=user["username"])
    return {"ok": True, "job_id": job.id, "filename": body.filename}


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


# ==================== 上传入库 ====================
@app.post("/api/ingest")
async def api_ingest(file: UploadFile = File(...), user: dict = Depends(current_user)):
    """上传合同文件到当前用户目录并入库（归该用户名下）。"""
    name = file.filename or "contract.txt"
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".txt", ".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 txt / pdf 格式")
    safe = os.path.basename(name.replace("\\", "/")).replace("/", "_")

    store = get_store()
    user_dir = bootstrap.ensure_user_dir(user["username"])
    # 与用户库内已有文件重名时自动加序号，避免覆盖
    final_name, counter = safe, 1
    base, e = os.path.splitext(safe)
    while store.get_file(user["id"], final_name) or os.path.exists(os.path.join(user_dir, final_name)):
        final_name = f"{base}_{counter}{e}"
        counter += 1

    dest = os.path.join(user_dir, final_name)
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)

    store.add_file(user["id"], final_name, "uploads", len(content))
    added = add_file_to_kb(dest, owner=user["username"])
    return {"ok": True, "saved": final_name, "added_chunks": added}


if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("  Contract AI · 本地化智能合同审查系统（网页版 · 用户版）")
    print(f"  模型：{LLM_MODEL_NAME}   服务：http://localhost:8000")
    print(f"  预置演示账号：demo / demo123（含保密协议等演示合同）")
    print("  Ctrl+C 退出")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
