# -*- coding: utf-8 -*-
"""
auth.py —— 用户认证工具
  * 密码：PBKDF2-HMAC-SHA256（标准库实现，不存明文、无第三方依赖）
  * 会话：登录发放随机 token（存 storage.sessions 表），请求带 Authorization 头
"""
import hashlib
import hmac
import os
import secrets

from storage import get_store

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
