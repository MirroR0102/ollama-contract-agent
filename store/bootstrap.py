# -*- coding: utf-8 -*-
"""
bootstrap.py —— 系统初始化与用户文件目录工具

* init_system()：启动时幂等初始化（以 storage.meta 标记只执行一次）
    - 建库建表（storage）
    - 预置演示账号 demo（密码 demo123）
    - 把 contracts/ 演示合同复制到 uploads/demo/，登记到 storage 并按 owner=demo 入库
    - 首次升级：清空旧的「无归属」向量数据后按 owner 重建，保证用户隔离干净
* 每个用户的合同文件统一存放：uploads/<用户名>/，物理与逻辑都互不串扰
* claim_root_orphans()：把 uploads/ 根目录遗留的旧文件认领给新注册用户（一次性）
"""
import os
import shutil

from core.config import (CHROMA_PATH, CONTRACTS_DIR, DB_ENGINE, DEMO_PASSWORD,
                         DEMO_USERNAME, EMBED_MODEL_NAME, LLM_MODEL_NAME)
from store import auth
from store.storage import get_store
from store.vector_store import add_file_to_kb, clear_db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_ROOT = os.path.join(BASE_DIR, "uploads")

# storage.meta 中的标记：完成过「用户体系预置」
SEED_KEY = "seed_users_v1"


def user_dir(username: str) -> str:
    """某用户的专属合同目录 uploads/<username>。"""
    return os.path.join(UPLOAD_ROOT, username)


def ensure_user_dir(username: str) -> str:
    d = user_dir(username)
    os.makedirs(d, exist_ok=True)
    return d


def _ensure_demo_user():
    """创建 / 取回预置演示账号。"""
    u = auth.register_user(DEMO_USERNAME, DEMO_PASSWORD, "演示账号（预置演示合同）")
    return u if u else get_store().get_user_by_name(DEMO_USERNAME)


def _brief(exc, limit: int = 160) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    msg = " ".join(str(msg).split())
    if len(msg) <= limit:
        return msg
    return msg[: limit - 1] + "…"


def _model_present(names, wanted):
    """对照 Ollama 模型列表。无法列模型时返回 None，而不是 False。"""
    if names is None:
        return None
    w = (wanted or "").strip()
    if not w:
        return False
    for raw in names:
        n = (raw or "").strip()
        if n == w or n.startswith(w + ":"):
            return True
    return False


def check_db() -> dict:
    """数据库探活。连接失败时返回 ok=false，不向外抛。"""
    engine = "mysql" if str(DB_ENGINE or "").lower() == "mysql" else "sqlite"
    try:
        get_store().ping()
        return {"ok": True, "engine": engine, "detail": "连接正常"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "engine": engine, "detail": _brief(e)}


def check_chroma() -> dict:
    """向量库目录是否可创建、可写。不打开 Chroma，也不连接嵌入模型。"""
    path = os.path.abspath(CHROMA_PATH or "./chroma_db")
    probe = os.path.join(path, ".health_write")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return {"ok": True, "path": path, "detail": "可写"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "path": path, "detail": _brief(e)}


def system_health() -> dict:
    """汇总 DB / Ollama / 模型 / 引擎 / Chroma。任一子系统失败都不抛异常。

    ok 只表示数据库可用：模型或 Ollama 不可用时仍为 true，细节看子字段。
    """
    from core import ollama_conn
    from core.llm_provider import provider_health

    db = check_db()
    try:
        ollama = ollama_conn.probe()
    except Exception as e:  # noqa: BLE001
        ollama = {"ok": False, "host": "", "detail": _brief(e), "models": None}
    names = ollama.get("models")
    if names is None:
        model_detail = ollama.get("detail") or "无法列出模型"
    else:
        model_detail = "已对照本机已安装模型"
    try:
        provider = provider_health()
    except Exception as e:  # noqa: BLE001
        provider = {
            "provider": "local",
            "cloud_configured": False,
            "cloud": {"ready": False, "detail": _brief(e)},
        }
    return {
        "ok": bool(db.get("ok")),
        "db": {"ok": bool(db.get("ok")), "engine": db.get("engine"), "detail": db.get("detail") or ""},
        "ollama": {
            "ok": bool(ollama.get("ok")),
            "host": ollama.get("host") or "",
            "detail": ollama.get("detail") or "",
        },
        "models": {
            "llm": LLM_MODEL_NAME,
            "embed": EMBED_MODEL_NAME,
            "llm_present": _model_present(names, LLM_MODEL_NAME),
            "embed_present": _model_present(names, EMBED_MODEL_NAME),
            "detail": model_detail,
        },
        "llm_provider": provider,
        "chroma": check_chroma(),
    }


def print_startup_summary() -> None:
    """启动时打印一行自检。Ollama / Chroma 失败只警告，不中断启动。"""
    try:
        h = system_health()
    except Exception as e:  # noqa: BLE001
        print(f"[自检] 警告：自检未完成（{_brief(e)}）", flush=True)
        return
    db = h.get("db") or {}
    ol = h.get("ollama") or {}
    prov = h.get("llm_provider") or {}
    ch = h.get("chroma") or {}
    cloud = "已配置" if prov.get("cloud_configured") else "未配置"
    print(
        "[自检] 数据库 {eng} {dbok} | Ollama {olok} | 引擎 {p}（云端{cloud}） | Chroma {chok}".format(
            eng=db.get("engine") or "?",
            dbok="正常" if db.get("ok") else "异常",
            olok="可达" if ol.get("ok") else "不可达",
            p=prov.get("provider") or "?",
            cloud=cloud,
            chok="可写" if ch.get("ok") else "不可写",
        ),
        flush=True,
    )
    if not db.get("ok"):
        print(f"[自检] 警告：数据库不可用：{db.get('detail') or '未知原因'}", flush=True)
    if not ol.get("ok"):
        print("[自检] 警告：Ollama 不可达。本地模型与嵌入暂不可用；"
              "使用云端引擎时，生成类功能仍可继续。详见 /api/health", flush=True)


def init_system() -> None:
    """启动初始化（幂等）：预置演示账号并把演示合同归入其名下。"""
    store = get_store()
    store.init_schema()
    os.makedirs(UPLOAD_ROOT, exist_ok=True)

    if store.get_meta(SEED_KEY) == "1":
        auth.ensure_bootstrap_admin()
        print_startup_summary()
        return  # 已完成过初始化

    demo = _ensure_demo_user()
    if not demo:
        raise RuntimeError("初始化失败：无法创建演示账号")
    demo_dir = ensure_user_dir(DEMO_USERNAME)

    # 1) contracts/ 演示合同 → uploads/demo/（复制，保留原 contracts 供命令行使用）
    copied = []
    if os.path.isdir(CONTRACTS_DIR):
        for fn in sorted(os.listdir(CONTRACTS_DIR)):
            if fn.lower().endswith((".txt", ".pdf")):
                src = os.path.join(CONTRACTS_DIR, fn)
                dst = os.path.join(demo_dir, fn)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                copied.append(fn)

    # 2) 首次切换用户体系：清空旧的「无归属」向量数据，按 owner 重建，保证隔离干净
    clear_db()

    # 3) 登记到 storage（含合同域 contract_id）+ 按域入库
    for fn in copied:
        p = os.path.join(demo_dir, fn)
        rec = store.add_contract(demo["id"], fn, fn, "contracts",
                                 os.path.getsize(p), note="演示合同")
        add_file_to_kb(p, owner=DEMO_USERNAME,
                       contract_id=rec["id"] if rec else "")
        if rec:
            try:
                store.set_contract_status(demo["id"], rec["id"], "ready")
            except Exception as e:  # noqa: BLE001
                print(f"[status] 演示合同置为 ready 失败：{e}")

    store.set_meta(SEED_KEY, "1")
    auth.ensure_bootstrap_admin()
    print(f"[init] 预置演示账号 {DEMO_USERNAME} 完成：{len(copied)} 份演示合同已归入并入库")
    print_startup_summary()


def list_root_orphans() -> list:
    """uploads/ 根目录下待认领的旧文件（非用户子目录），供新用户注册时认领。"""
    out = []
    if os.path.isdir(UPLOAD_ROOT):
        for fn in os.listdir(UPLOAD_ROOT):
            p = os.path.join(UPLOAD_ROOT, fn)
            if os.path.isfile(p) and fn.lower().endswith((".txt", ".pdf")):
                out.append(fn)
    return out


def claim_root_orphans(user) -> int:
    """把上传根目录遗留文件认领给指定用户（注册时调用；认领一次后根目录即空）。"""
    store = get_store()
    uname = user["username"]
    d = ensure_user_dir(uname)
    claimed = 0
    for fn in list_root_orphans():
        src = os.path.join(UPLOAD_ROOT, fn)
        dst = os.path.join(d, fn)
        try:
            if not os.path.exists(dst):
                os.replace(src, dst)
            rec = store.add_contract(user["id"], fn, fn, "uploads",
                                     os.path.getsize(dst), note="历史认领文件")
            add_file_to_kb(dst, owner=uname,
                           contract_id=rec["id"] if rec else "")
            if rec:
                try:
                    store.set_contract_status(user["id"], rec["id"], "ready")
                except Exception as e:  # noqa: BLE001
                    print(f"[status] 认领合同置为 ready 失败：{e}")
            claimed += 1
        except Exception as e:  # noqa: BLE001
            print(f"[claim] 认领 {fn} 失败：{e}")
    if claimed:
        print(f"[claim] 上传库遗留文件 {claimed} 份已归入用户 {uname}")
    return claimed


def resolve_user_file(username: str, name: str):
    """按用户名定位其合同文件绝对路径；不存在返回 None。含路径穿越防护。"""
    safe = os.path.basename(name.replace("\\", "/"))
    if not safe:
        return None
    p = os.path.join(user_dir(username), safe)
    return p if os.path.isfile(p) else None
