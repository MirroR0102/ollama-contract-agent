# -*- coding: utf-8 -*-
"""
app.py —— Contract AI 网页版后端（FastAPI + SSE 流式）
将本地化合同审查系统的全部能力封装为 HTTP 接口：
  知识库问答 / Agent 对话 / 合同审查 / 要素抽取 / 合同入库
全程本地 Ollama 推理，零云依赖。
启动：python app.py  →  http://localhost:8000
"""
import asyncio
import json
import os
import queue
import threading
import time

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from pydantic import BaseModel

from agent_run import kb_agent
from config import CONTRACTS_DIR, LLM_MODEL_NAME
from contract_analyzer import REVIEW_DIMENSIONS, analyze_dimension
from contract_kb import _KB_PROMPT, _format_context, llm
from element_extractor import extract_elements
from jobs import JobAbort, create_job, get_job, keepalive_all
import session_context
from vector_store import add_file_to_kb, db_stats, get_retriever

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app = FastAPI(title="Contract AI · 本地化智能合同审查系统")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Ollama 单模型串行：全局锁避免并发推理错乱
MODEL_LOCK = threading.Lock()

_BUSY_MSG = "系统正忙：正在执行合同审查等后台推理任务，请稍候再试。"


def _try_lock() -> bool:
    """即时请求尝试获取模型锁（拿不到即提示忙，避免无限排队假死）。"""
    return MODEL_LOCK.acquire(timeout=3.0)


@app.middleware("http")
async def _keepalive_middleware(request, call_next):
    """用户仍在发起请求 → 浏览器仍在使用 → 给后台任务续期（避免误中止）。"""
    keepalive_all()
    return await call_next(request)


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """页面与静态资源禁用缓存：改版后刷新即可看到最新版本（避免浏览器命中旧 HTML/JS/CSS）。"""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path in ("/", "/kb", "/review", "/ingest"):
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
    worker 结束时自动补发 done 事件；异常自动补发 error 事件。

    关键：客户端断开（刷新 / 关闭页面）时，本生成器被 asyncio 取消，
    finally 中会置 stop 事件；worker 在每轮循环 / 每个 token 处检查
    stop 即可及时中止推理，尽快释放 MODEL_LOCK，避免整站假死。
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
                await asyncio.sleep(0.03)  # 轮询间隔：让取消能被及时感知
                continue
            if etype == "__end__":
                break
            yield _sse({"type": etype, "data": payload})
        yield _sse({"type": "done"})
    finally:
        stop.set()  # 正常结束或客户端断开都会通知 worker 停止


def sse_response(worker) -> StreamingResponse:
    return StreamingResponse(
        sse_generator(worker),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================== 工具函数 ====================
def _list_all_files() -> list:
    """列出 contracts 与 uploads 两个目录的合同文件。"""
    files = []
    for d in (CONTRACTS_DIR, UPLOAD_DIR):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith((".txt", ".pdf")):
                    files.append({"name": f, "dir": os.path.basename(d)})
    return files


def _resolve_anywhere(name: str):
    """在 contracts/uploads 目录定位文件，返回绝对路径或 None。"""
    for d in (CONTRACTS_DIR, UPLOAD_DIR):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    if os.path.isfile(name):
        return os.path.abspath(name)
    return None


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


# ==================== 数据接口 ====================
@app.get("/api/files")
def api_files():
    """合同清单 + 系统状态（各页面共用）。"""
    return {
        "files": _list_all_files(),
        "model": LLM_MODEL_NAME,
        "stats": db_stats(),
    }


# ==================== SSE 流式接口 ====================
class KBQueryBody(BaseModel):
    question: str
    top_k: int = 3  # 检索返回片段数（默认 top 3）
    sources: list[str] = []  # 上下文合同范围（空 = 全部合同）


@app.post("/api/kb/query")
def api_kb_query(body: KBQueryBody):
    """知识库 RAG 问答（纯检索 + 流式回答，带证据片段）。
    - sources 非空时仅在这些合同文件内检索；空 = 全部合同。"""

    def worker(send, stop):
        if not _try_lock():
            send("error", {"message": _BUSY_MSG})
            return
        try:
            retriever = get_retriever(body.top_k, sources=body.sources or None)
            docs = retriever.invoke(body.question)
            if not docs:
                if body.sources:
                    send("message", {"text": "当前选定的上下文合同范围内未检索到相关内容：请确认所选合同已入库，或改为使用「全部合同」后重试。"})
                else:
                    send("message", {"text": "知识库未检索到相关合同内容，请先到「合同入库」页导入合同。"})
                return
            # ① 证据片段
            send("evidence", {
                "items": [
                    {"index": i + 1,
                     "source": d.metadata.get("source", "未知"),
                     "content": d.page_content}
                    for i, d in enumerate(docs)
                ]
            })
            # ② 流式回答
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
def api_chat(body: ChatBody):
    """Agent 多轮对话（工具调用事件 + 流式回答）。
    发送前把用户选定的上下文合同范围登记到该会话，Agent 检索工具据此限定范围。"""

    def worker(send, stop):
        if not _try_lock():
            send("error", {"message": _BUSY_MSG})
            return
        try:
            # 登记该会话的上下文合同范围（空 = 全部），供 Agent 检索工具读取
            session_context.set_sources(body.thread_id, body.sources or None)
            tool_names: dict = {}
            for chunk, metadata in kb_agent.stream(
                {"messages": [HumanMessage(content=body.message)]},
                config={"configurable": {"thread_id": body.thread_id}},
                stream_mode="messages",
            ):
                if stop.is_set():
                    return
                # 工具调用决策
                tcc = getattr(chunk, "tool_call_chunks", None)
                if tcc:
                    for tc in tcc:
                        idx = tc.get("index", 0)
                        piece = (tc.get("name") or "").strip()
                        if piece and idx not in tool_names:
                            tool_names[idx] = piece
                            send("tool_call", {"name": piece})
                # 文本 / 工具结果
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
    """后台任务等待模型锁：等待期间若任务应停止（中止/用户离开）则退出返回 False。"""
    while True:
        if MODEL_LOCK.acquire(timeout=1.0):
            return True
        if job.should_stop():
            return False


def run_review_job(job):
    """合同 8 维度审查任务体：逐维度写入 job 状态，任意页面可订阅。"""
    if not _wait_model_lock(job):
        return
    try:
        path = _resolve_anywhere(job.filename)
        if not path:
            job.status = "error"
            job.error = f"未找到合同文件：{job.filename}"
            return
        add_file_to_kb(path)  # 确保已入库（幂等）
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
                os.path.basename(path), dim, stream=False, emit=emit,
            )
            job.results.append(result)
    finally:
        MODEL_LOCK.release()


def run_extract_job(job):
    """合同要素抽取任务体。"""
    if not _wait_model_lock(job):
        return
    try:
        path = _resolve_anywhere(job.filename)
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
def api_analyze(body: FileBody):
    """提交合同 8 维度审查任务：立即返回 job_id，进度经 /api/jobs/<id>/stream 订阅。"""
    path = _resolve_anywhere(body.filename)
    if not path:
        raise HTTPException(status_code=404, detail=f"未找到合同文件：{body.filename}")
    job = create_job("review", body.filename, run_review_job)
    return {
        "ok": True,
        "job_id": job.id,
        "filename": body.filename,
        "total_dims": len(REVIEW_DIMENSIONS),
    }


@app.post("/api/extract")
def api_extract(body: FileBody):
    """提交合同要素抽取任务：立即返回 job_id。"""
    path = _resolve_anywhere(body.filename)
    if not path:
        raise HTTPException(status_code=404, detail=f"未找到合同文件：{body.filename}")
    job = create_job("extract", body.filename, run_extract_job)
    return {"ok": True, "job_id": job.id, "filename": body.filename}


@app.get("/api/jobs/{job_id}/status")
def api_job_status(job_id: str):
    """查询任务状态（页面刷新 / 重新打开时恢复进度用）。"""
    job = get_job(job_id)
    if not job:
        return {"exists": False}
    return {"exists": True, **job.snapshot()}


@app.post("/api/jobs/{job_id}/leave")
def api_job_leave(job_id: str):
    """页面离开时上报（sendBeacon）：后台由此重算「离开超时」。"""
    job = get_job(job_id)
    if job:
        job.touch()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/abort")
def api_job_abort(job_id: str):
    """彻底终止后台任务（不可恢复）。"""
    job = get_job(job_id)
    if job:
        job.abort("已手动终止")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pause")
def api_job_pause(job_id: str):
    """暂停任务（保留进度，可继续）。"""
    job = get_job(job_id)
    if job:
        job.pause()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def api_job_resume(job_id: str):
    """从暂停处继续任务。"""
    job = get_job(job_id)
    if job:
        job.resume()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str):
    """订阅任务进度（SSE）。支持多端同时订阅；单个页面断开不影响任务继续。"""
    job = get_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"detail": "任务不存在或已过期"})

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
async def api_ingest(file: UploadFile = File(...)):
    """上传合同文件并入库。"""
    name = file.filename or "contract.txt"
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".txt", ".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 txt / pdf 格式")
    safe = os.path.basename(name.replace("\\", "/")).replace("/", "_")
    base, e = os.path.splitext(safe)
    dest = os.path.join(UPLOAD_DIR, safe)
    counter = 1
    while os.path.exists(dest):
        dest = os.path.join(UPLOAD_DIR, f"{base}_{counter}{e}")
        counter += 1

    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)

    added = add_file_to_kb(dest)
    return {"ok": True, "saved": os.path.basename(dest), "added_chunks": added}


if __name__ == "__main__":
    import uvicorn

    print("=" * 56)
    print("  Contract AI · 本地化智能合同审查系统（网页版）")
    print(f"  模型：{LLM_MODEL_NAME}   服务：http://localhost:8000")
    print("  Ctrl+C 退出")
    print("=" * 56)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
