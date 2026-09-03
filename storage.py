# -*- coding: utf-8 -*-
"""
storage.py —— 用户系统与合同库归属的统一存储层

同一套业务接口，两个后端实现：
  * SqliteStore：DB_ENGINE=sqlite（默认，本地文件 contract_ai.db，零配置）
  * MysqlStore ：DB_ENGINE=mysql（需在 .env 配置 DB_HOST/DB_USER/DB_PASSWORD/DB_NAME）

表结构（两后端完全一致）：
  users         用户（id/username/password_hash/display_name/created_at）
  sessions      token 会话（token/user_id/created_at）
  contract_files 合同归属（user_id/name/dir/size/created_at，user+name 唯一）
  meta          键值（记录初始化/预置状态等）
"""
import os
import sqlite3
import threading
import time

from config import (
    DB_ENGINE, DB_PATH, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
)

# 各模块共享同一 store 实例
_store = None
_store_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """存储层统一接口（子类实现）。"""

    def init_schema(self):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    # ---------- 用户 ----------
    def create_user(self, username, password_hash, display_name="") -> int:
        raise NotImplementedError

    def get_user_by_name(self, username):
        raise NotImplementedError

    def get_user_by_id(self, user_id):
        raise NotImplementedError

    # ---------- 会话 ----------
    def create_session(self, token, user_id):
        raise NotImplementedError

    def delete_session(self, token):
        raise NotImplementedError

    def get_session_user(self, token):
        raise NotImplementedError

    # ---------- 合同文件归属 ----------
    def add_file(self, user_id, name, dir_name="uploads", size=0) -> bool:
        """登记一份合同到某用户名下；已存在(同用户同名)返回 False。"""
        raise NotImplementedError

    def list_files(self, user_id):
        """返回某用户的合同清单 [{name,dir,size,created_at}]。"""
        raise NotImplementedError

    def get_file(self, user_id, name):
        """查某用户名下是否存在该文件，返回行 dict 或 None。"""
        raise NotImplementedError

    def delete_file(self, user_id, name) -> bool:
        raise NotImplementedError

    def move_files_to(self, usernames, user_id):
        """把 usernames（上传根目录中待认领的旧文件清单）登记到新用户。"""
        raise NotImplementedError

    # ---------- 元信息 ----------
    def set_meta(self, key, value):
        raise NotImplementedError

    def get_meta(self, key, default=None):
        raise NotImplementedError


# ==================== SQLite 实现 ====================
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS contract_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  dir TEXT DEFAULT 'uploads',
  size INTEGER DEFAULT 0,
  created_at TEXT,
  UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""


class SqliteStore(Store):
    def __init__(self, path=DB_PATH):
        self._path = path
        self._lock = threading.Lock()
        # check_same_thread=False：FastAPI 多线程访问，靠 _lock 串行化
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    def init_schema(self):
        with self._lock:
            self._conn.executescript(_SQLITE_SCHEMA)
            self._conn.commit()

    def _row(self, r):
        return dict(r) if r is not None else None

    def create_user(self, username, password_hash, display_name=""):
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO users(username,password_hash,display_name,created_at) "
                "VALUES(?,?,?,?)",
                (username, password_hash, display_name, _now()),
            )
            self._conn.commit()
            return cur.lastrowid if cur.rowcount else -1

    def get_user_by_name(self, username):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM users WHERE username=?", (username,))
            return self._row(cur.fetchone())

    def get_user_by_id(self, user_id):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
            return self._row(cur.fetchone())

    def create_session(self, token, user_id):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                (token, user_id, _now()),
            )
            self._conn.commit()

    def delete_session(self, token):
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            self._conn.commit()

    def get_session_user(self, token):
        with self._lock:
            cur = self._conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                (token,),
            )
            return self._row(cur.fetchone())

    def add_file(self, user_id, name, dir_name="uploads", size=0):
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO contract_files(user_id,name,dir,size,created_at) "
                "VALUES(?,?,?,?,?)",
                (user_id, name, dir_name, size, _now()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_files(self, user_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT name,dir,size,created_at FROM contract_files WHERE user_id=? "
                "ORDER BY created_at DESC, id DESC",
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_file(self, user_id, name):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM contract_files WHERE user_id=? AND name=?", (user_id, name)
            )
            return self._row(cur.fetchone())

    def delete_file(self, user_id, name):
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM contract_files WHERE user_id=? AND name=?", (user_id, name)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def move_files_to(self, usernames, user_id):
        added = 0
        with self._lock:
            for name in usernames:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO contract_files(user_id,name,dir,size,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id, name, "uploads", 0, _now()),
                )
                added += cur.rowcount
            self._conn.commit()
        return added

    def set_meta(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, str(value))
            )
            self._conn.commit()

    def get_meta(self, key, default=None):
        with self._lock:
            cur = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,))
            r = cur.fetchone()
            return r["v"] if r is not None else default


# ==================== MySQL 实现 ====================
_MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(64) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  display_name VARCHAR(64),
  created_at VARCHAR(32)
) DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS sessions (
  token VARCHAR(64) PRIMARY KEY,
  user_id INT NOT NULL,
  created_at VARCHAR(32)
) DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS contract_files (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NOT NULL,
  name VARCHAR(255) NOT NULL,
  dir VARCHAR(32) DEFAULT 'uploads',
  size INT DEFAULT 0,
  created_at VARCHAR(32),
  UNIQUE KEY uk_user_file (user_id, name)
) DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS meta (
  k VARCHAR(64) PRIMARY KEY,
  v VARCHAR(255)
) DEFAULT CHARSET=utf8mb4;
"""


class MysqlStore(Store):
    def __init__(self, host=DB_HOST, port=DB_PORT, user=DB_USER,
                 password=DB_PASSWORD, db=DB_NAME):
        import pymysql  # 延迟导入：未安装时 SQLite 不受影响
        self._pymysql = pymysql
        self._cfg = dict(host=host, port=port, user=user, password=password,
                         charset="utf8mb4")
        self._db = db
        # 先确保数据库存在（此时不带 database，避免 Unknown database）
        conn = self._connect_raw()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db}` DEFAULT CHARACTER SET utf8mb4"
                )
            conn.commit()
        finally:
            conn.close()

    def _connect_raw(self):
        """连接但不指定数据库（用于建库）。"""
        return self._pymysql.connect(
            **self._cfg, autocommit=True,
            cursorclass=self._pymysql.cursors.DictCursor,
        )

    def _connect_db(self, db=None):
        return self._pymysql.connect(
            **self._cfg, database=db or self._db, autocommit=True,
            cursorclass=self._pymysql.cursors.DictCursor,
        )

    def _q(self, sql, args=()):
        """执行并提交，返回 (conn, affected_rows)。"""
        conn = self._connect_db()
        try:
            with conn.cursor() as cur:
                n = cur.execute(sql, args)
            return conn, n
        except Exception:
            conn.close()
            raise

    def _fetch(self, sql, args=(), one=False):
        conn = self._connect_db()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchone() if one else cur.fetchall()
        finally:
            conn.close()

    def init_schema(self):
        conn = self._connect_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
                for stmt in _MYSQL_SCHEMA.strip().split(";"):
                    if stmt.strip():
                        cur.execute(stmt)
            conn.commit()
        finally:
            conn.close()

    def create_user(self, username, password_hash, display_name=""):
        try:
            conn, n = self._q(
                "INSERT IGNORE INTO users(username,password_hash,display_name,created_at) "
                "VALUES(%s,%s,%s,%s)",
                (username, password_hash, display_name, _now()),
            )
            conn.close()
            if not n:
                return -1
            r = self._fetch(
                "SELECT id FROM users WHERE username=%s", (username,), one=True
            )
            return r["id"] if r else -1
        except Exception:
            return -1

    def get_user_by_name(self, username):
        return self._fetch(
            "SELECT * FROM users WHERE username=%s", (username,), one=True
        )

    def get_user_by_id(self, user_id):
        return self._fetch("SELECT * FROM users WHERE id=%s", (user_id,), one=True)

    def create_session(self, token, user_id):
        self._q(
            "REPLACE INTO sessions(token,user_id,created_at) VALUES(%s,%s,%s)",
            (token, user_id, _now()),
        )[0].close()

    def delete_session(self, token):
        self._q("DELETE FROM sessions WHERE token=%s", (token,))[0].close()

    def get_session_user(self, token):
        return self._fetch(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=%s",
            (token,),
            one=True,
        )

    def add_file(self, user_id, name, dir_name="uploads", size=0):
        try:
            conn, n = self._q(
                "INSERT IGNORE INTO contract_files(user_id,name,dir,size,created_at) "
                "VALUES(%s,%s,%s,%s,%s)",
                (user_id, name, dir_name, size, _now()),
            )
            conn.close()
            return n > 0
        except Exception:
            return False

    def list_files(self, user_id):
        return self._fetch(
            "SELECT name,dir,size,created_at FROM contract_files WHERE user_id=%s "
            "ORDER BY created_at DESC, id DESC",
            (user_id,),
        )

    def get_file(self, user_id, name):
        return self._fetch(
            "SELECT * FROM contract_files WHERE user_id=%s AND name=%s",
            (user_id, name),
            one=True,
        )

    def delete_file(self, user_id, name):
        conn, n = self._q(
            "DELETE FROM contract_files WHERE user_id=%s AND name=%s", (user_id, name)
        )
        conn.close()
        return n > 0

    def move_files_to(self, usernames, user_id):
        added = 0
        for name in usernames:
            if self.add_file(user_id, name, "uploads", 0):
                added += 1
        return added

    def set_meta(self, key, value):
        self._q(
            "REPLACE INTO meta(k,v) VALUES(%s,%s)", (key, str(value))
        )[0].close()

    def get_meta(self, key, default=None):
        r = self._fetch("SELECT v FROM meta WHERE k=%s", (key,), one=True)
        return r["v"] if r else default


# ==================== 工厂 ====================
def get_store() -> Store:
    """返回全局存储实例（按 DB_ENGINE 选择后端，线程安全单例）。"""
    global _store
    with _store_lock:
        if _store is None:
            if DB_ENGINE == "mysql":
                _store = MysqlStore()
            else:
                _store = SqliteStore()
            _store.init_schema()
        return _store


def reset_store():
    """仅供测试：强制重建 store。"""
    global _store
    with _store_lock:
        _store = None
    return get_store()
