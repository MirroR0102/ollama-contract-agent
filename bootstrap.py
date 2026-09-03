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

import auth
from config import CONTRACTS_DIR, DEMO_PASSWORD, DEMO_USERNAME
from storage import get_store
from vector_store import add_file_to_kb, clear_db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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


def init_system() -> None:
    """启动初始化（幂等）：预置演示账号并把演示合同归入其名下。"""
    store = get_store()
    store.init_schema()
    os.makedirs(UPLOAD_ROOT, exist_ok=True)

    if store.get_meta(SEED_KEY) == "1":
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

    # 3) 登记到 storage + 按 owner 入库
    for fn in copied:
        p = os.path.join(demo_dir, fn)
        store.add_file(demo["id"], fn, "contracts", os.path.getsize(p))
        add_file_to_kb(p, owner=DEMO_USERNAME)

    store.set_meta(SEED_KEY, "1")
    print(f"[init] 预置演示账号 {DEMO_USERNAME} 完成：{len(copied)} 份演示合同已归入并入库")


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
            store.add_file(user["id"], fn, "uploads", os.path.getsize(dst))
            add_file_to_kb(dst, owner=uname)
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
