# -*- coding: utf-8 -*-
"""
llm_provider.py —— 双模型引擎网关（本地 Ollama ⇄ 云端 OpenAI 兼容 API）

背景：本地 qwen2.5:7b 满足「数据不出域」，但生成质量有限；很多场景希望接入
联网大模型（DeepSeek / 通义 / Kimi / 硅基流动 等，均为 OpenAI 兼容接口）获得
更好的审查、问答与起草质量。本模块把「用哪个模型」集中管理，业务代码只调用
本模块的 get_llm() / retry_llm_call()，无需关心当前是本地还是云端：

  * provider = local  → 本地 Ollama（默认，全程离线；连接自愈见 ollama_conn）
  * provider = cloud  → 云端 OpenAI 兼容 API（密钥可来自 .env，也可由用户在
                        网页端「模型设置」中填写个人 Key，按用户隔离）

选择机制（就近优先）：本线程/请求上下文（contextvars）> .env 默认值。
网页端每次请求 / 后台任务开始时，由 app.py 调用 configure() 注入当前用户偏好；
命令行不注入，恒用 .env 默认（LLM_PROVIDER）。

嵌入模型固定使用本地 bge-m3（不走云端），原因见 README「为什么嵌入保持本地」：
 ① bge-m3 是开源顶尖多语言嵌入模型，中文合同检索已足够；
 ② 切换嵌入模型会改变向量空间，必须全量重建向量库，且合同全文将上传云端；
 ③ DeepSeek 等生成类平台并不提供嵌入接口。
"""
import contextvars
import threading
import time

from core import ollama_conn
from core.config import (CLOUD_API_KEY, CLOUD_BASE_URL, CLOUD_MODEL_NAME,
                         CLOUD_TIMEOUT, LLM_DEFAULT_PROVIDER)

# ---------- 引擎常量 ----------
PROVIDER_LOCAL = "local"
PROVIDER_CLOUD = "cloud"
VALID_PROVIDERS = (PROVIDER_LOCAL, PROVIDER_CLOUD)
DEFAULT_PROVIDER = (LLM_DEFAULT_PROVIDER if LLM_DEFAULT_PROVIDER in VALID_PROVIDERS
                    else PROVIDER_LOCAL)

# 重试策略：沿用本地模块的节奏（重建客户端后重试）
MAX_RETRY = ollama_conn.MAX_RETRY
BASE_DELAY = ollama_conn.BASE_DELAY

# ---------- 上下文选择（contextvars —— 线程/请求级隔离） ----------
# 说明：新建线程（SSE worker / 后台任务）不会自动继承父线程的上下文，
# 因此 app.py 会在每个 worker / 任务开头重新调用 configure() 注入用户偏好。
_provider_var = contextvars.ContextVar("llm_provider", default=None)
_cloud_override_var = contextvars.ContextVar("cloud_override", default=None)


def set_provider(name) -> str:
    """设置当前线程/请求上下文的模型引擎（local / cloud）；非法值回退默认。"""
    n = (name or "").strip().lower()
    if n not in VALID_PROVIDERS:
        n = DEFAULT_PROVIDER
    _provider_var.set(n)
    return n


def get_provider() -> str:
    """当前生效的模型引擎：local / cloud。"""
    return _provider_var.get() or DEFAULT_PROVIDER


def set_cloud_override(api_key: str = None, model: str = None) -> None:
    """设置本上下文的云端覆盖参数（用户个人 Key / 个人模型名）；空 = 用 .env。"""
    ov = {}
    if (api_key or "").strip():
        ov["api_key"] = api_key.strip()
    if (model or "").strip():
        ov["model"] = model.strip()
    _cloud_override_var.set(ov or None)


def configure(provider=None, api_key=None, model=None) -> str:
    """一次性配置本上下文（引擎 + 云端覆盖）；网页端按登录用户偏好调用。"""
    set_provider(provider)
    set_cloud_override(api_key, model)
    return get_provider()


def effective_cloud_cfg() -> dict:
    """解析「当前生效」的云端配置：个人覆盖优先于 .env。仅供内部使用。"""
    ov = _cloud_override_var.get() or {}
    env_key = (CLOUD_API_KEY or "").strip()
    key = ov.get("api_key") or env_key
    return {
        "base_url": (CLOUD_BASE_URL or "https://api.deepseek.com/v1").rstrip("/"),
        "api_key": key,
        "model": ov.get("model") or CLOUD_MODEL_NAME or "deepseek-chat",
        "ready": bool(key),
        "key_source": "user" if ov.get("api_key") else ("env" if env_key else "none"),
    }


def cloud_status(personal_key: str = None, personal_model: str = None) -> dict:
    """给网页端展示的云端状态（绝不返回密钥明文，只给可见信息与来源标记）。"""
    env_key = (CLOUD_API_KEY or "").strip()
    pk = (personal_key or "").strip()
    key = pk or env_key
    return {
        "base_url": (CLOUD_BASE_URL or "").rstrip("/"),
        "model": (personal_model or "").strip() or (CLOUD_MODEL_NAME or "deepseek-chat"),
        "ready": bool(key),
        "key_source": "user" if pk else ("env" if env_key else "none"),
        "has_personal_key": bool(pk),
        "has_env_key": bool(env_key),
    }


# ---------- 云端客户端（懒加载构建 + 失效重建） ----------
_cloud_lock = threading.Lock()
_cloud_llm = None
_cloud_sig = None  # 构建时使用的 (base_url, api_key, model)；签名变化自动重建

_NO_KEY_MSG = ("云端模型未配置 API Key：请点击右上角 ⚙️「模型设置」填写（或联系管理员"
               "在 .env 配置 CLOUD_API_KEY），也可以先切换回本地模型。")


def _build_cloud_llm():
    """构造全新云端客户端（ChatOpenAI，OpenAI 兼容协议）。"""
    cfg = effective_cloud_cfg()
    if not cfg["ready"]:
        raise RuntimeError(_NO_KEY_MSG)
    from langchain_openai import ChatOpenAI  # 懒加载：未安装时不影响本地模式
    return ChatOpenAI(
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=0,        # 与本地一致：审查/抽取要求确定性输出
        timeout=CLOUD_TIMEOUT,
        max_retries=0,        # 重试策略由本模块统一控制（避免双层重试）
        max_tokens=4096,      # 云端单次输出上限（对应本地 num_predict）
    )


def _get_cloud_llm():
    """取云端客户端；配置签名（Key/模型/地址）变化时自动重建。"""
    global _cloud_llm, _cloud_sig
    cfg = effective_cloud_cfg()
    sig = (cfg["base_url"], cfg["api_key"], cfg["model"])
    with _cloud_lock:
        if _cloud_llm is None or _cloud_sig != sig:
            _cloud_llm = _build_cloud_llm()
            _cloud_sig = sig
        return _cloud_llm


# ---------- 统一出口（业务代码只用这几个） ----------
def get_llm():
    """返回当前上下文的 LLM 实例（本地 ChatOllama / 云端 ChatOpenAI）。"""
    if get_provider() == PROVIDER_CLOUD:
        return _get_cloud_llm()
    return ollama_conn.get_llm()


def reset_llm():
    """重建「当前引擎」的客户端实例（连接失效后由重试逻辑调用）。"""
    global _cloud_llm
    if get_provider() == PROVIDER_CLOUD:
        with _cloud_lock:
            _cloud_llm = None  # 清缓存，下次 get_llm 重新构建
        print("  [重连] 已重建云端 ChatOpenAI 客户端实例", flush=True)
        return get_llm()
    return ollama_conn.reset_llm()


def get_emb():
    """Embedding 实例（固定本地 bge-m3，不走云端；见模块 docstring）。"""
    return ollama_conn.get_emb()


def reset_emb():
    """重建本地 Embedding 实例（仍由 ollama_conn 负责）。"""
    return ollama_conn.reset_emb()


# ---------- 连接错误的统一判定与重试 ----------
# 云端（openai SDK / httpx）连接类错误的类名与关键字；
# 注意：401/402/429 等业务错误不算连接错误——重建客户端重试无意义，应如实上抛。
_CLOUD_CONN_CLASSES = ("APIConnectionError", "APITimeoutError", "ConnectError",
                       "ConnectTimeout", "ReadTimeout", "WriteTimeout",
                       "RemoteProtocolError", "TransportError")
_CLOUD_CONN_KEYS = ("connection error", "connection refused", "connection reset",
                    "connect timeout", "read timeout", "timed out", "timeout",
                    "server disconnected", "econnrefused", "econnreset",
                    "getaddrinfo failed", "max retries exceeded", "unreachable",
                    "network", "代理")


def is_conn_error(exc) -> bool:
    """是否属于「连接失效 / 网络不可达」类错误（可重建客户端后重试）。"""
    if ollama_conn.is_conn_error(exc):
        return True
    for klass in type(exc).__mro__:
        if klass.__name__ in _CLOUD_CONN_CLASSES:
            return True
    msg = str(exc).lower()
    return any(k in msg for k in _CLOUD_CONN_KEYS)


def retry_llm_call(fn, retries: int = MAX_RETRY):
    """执行一次 LLM 调用 fn()；若抛连接类错误，重建「当前引擎」实例后重试。

    fn 必须是「每次调用都重新 get_llm()」的闭包（重试时会拿到重建后的新实例）。
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt < retries and is_conn_error(e):
                time.sleep(BASE_DELAY * (attempt + 1))
                reset_llm()
                continue
            raise
    raise RuntimeError("unreachable")
