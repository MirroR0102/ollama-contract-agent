# -*- coding: utf-8 -*-
"""
auth.py —— 用户认证（密码哈希 + token 会话）

密码不存明文。hash_password 用标准库 hashlib.pbkdf2_hmac：
  算法 SHA-256，盐 16 字节随机数，迭代 _PBKDF2_ITER（20 万）次。
  落库格式是「盐的 hex $ 派生密钥的 hex」，例如 a1b2...$9f3c...。
  校验时用同一盐重算，再用 hmac.compare_digest 比较，避免逐字符提前返回。

会话不走 cookie 框架。login 成功后 secrets.token_hex(24) 得到 48 位 hex，
写入 storage.sessions，和 user_id 绑定。agent/app.py 的 current_user
每次请求调用 user_by_token。logout 删的是这一行 session，不改密码。

本模块不管合同归属。用户名和密码校验通过后，合同隔离看 storage 与
向量库的 owner。注册本身不创建 uploads 目录，那个在实际上传 / bootstrap 时做。
"""
import hashlib
import hmac
import os
import secrets

from store.storage import get_store

_PBKDF2_ITER = 200_000


def hash_password(password: str, salt_hex: str = "") -> str:
    """生成可存储的密码哈希：salt$hash（均为 hex）。"""
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码，防时序攻击比对。"""
    try:
        salt_hex, _ = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    calc = hash_password(password, salt_hex)
    return hmac.compare_digest(calc, stored)


def register_user(username: str, password: str, display_name: str = "") -> dict:
    """注册新用户；用户名已存在返回 None。成功后返回用户 dict。"""
    store = get_store()
    username = username.strip()
    if not username or not password:
        return None
    uid = store.create_user(username, hash_password(password), display_name or username)
    if uid < 0:
        return None
    return store.get_user_by_id(uid)


def login(username: str, password: str) -> dict:
    """校验并登录；成功返回 {token, username, display_name}，失败返回 None。"""
    store = get_store()
    user = store.get_user_by_name(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        return None
    token = secrets.token_hex(24)
    store.create_session(token, user["id"])
    return {
        "token": token,
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
    }


def logout(token: str) -> None:
    if token:
        get_store().delete_session(token)


def user_by_token(token: str):
    """按 token 取当前登录用户 dict（无则 None）。"""
    if not token:
        return None
    return get_store().get_session_user(token)
