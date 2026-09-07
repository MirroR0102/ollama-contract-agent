# -*- coding: utf-8 -*-
"""
storage.py —— 用户系统 / 合同库 / 文件夹分组 的统一存储层（v2）

同一套业务接口，两个后端实现：
  * SqliteStore：DB_ENGINE=sqlite（默认，本地文件 contract_ai.db，零配置）
  * MysqlStore ：DB_ENGINE=mysql（需在 .env 配置 DB_HOST/DB_USER/DB_PASSWORD/DB_NAME）

表结构（两后端一致）：
  users           用户（id/username/password_hash/display_name/created_at）
  sessions        token 会话
  folders         合同库文件夹（id/user_id/name/created_at，user+name 唯一）
  contract_files  合同记录（id/user_id/name[显示标题,可重名]/store_name[磁盘唯一名]
                   /dir/size/note[备注]/folder_id[所属文件夹]/created_at，
                   user+store_name 唯一 —— 允许同名标题各自独立成库）
  meta            键值（记录初始化/预置状态/迁移版本等）

v2 迁移：自动补列（note/store_name/folder_id）、建 folders、为既有用户建默认
文件夹“我的合同”并把旧合同回填进去、放开「标题唯一」约束（旧库为 user+name
唯一，改为 user+store_name 唯一）。
"""
import os
import sqlite3
import threading
import time

from core.config import (
    DB_ENGINE, DB_PATH, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
)

# 各模块共享同一 store 实例
_store = None
_store_lock = threading.Lock()

DEFAULT_FOLDER_NAME = "我的合同"
_SCHEMA_VERSION_KEY = "schema_version"
_SCHEMA_VERSION = "2"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """存储层统一接口（子类实现）。"""

    def init_schema(self):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def migrate(self):  # pragma: no cover - 由子类实现
        """v1 → v2 数据迁移（建 folders、补列、回填默认文件夹）。"""
        raise NotImplementedError

    # ---------- 用户 ----------
    def create_user(self, username, password_hash, display_name="") -> int:
        raise NotImplementedError

    def get_user_by_name(self, username):
        raise NotImplementedError

    def get_user_by_id(self, user_id):
        raise NotImplementedError

    def list_users(self):
        """枚举全部用户（维护 / 管理用）。"""
        raise NotImplementedError

    # ---------- 会话 ----------
    def create_session(self, token, user_id):
        raise NotImplementedError

    def delete_session(self, token):
        raise NotImplementedError

    def get_session_user(self, token):
        raise NotImplementedError

    # ---------- 文件夹（合同库分组） ----------
    def create_folder(self, user_id, name):
        """新建文件夹；同名已存在返回 None。"""
        raise NotImplementedError

    def get_folder(self, user_id, folder_id):
        raise NotImplementedError

    def list_folders(self, user_id):
        """返回该用户文件夹清单，含每文件夹下合同数 cnt。"""
        raise NotImplementedError

    def ensure_default_folder(self, user_id):
        """确保该用户存在默认文件夹，返回其 row。"""
        raise NotImplementedError

    def delete_folder_row(self, user_id, folder_id) -> bool:
        """仅删除文件夹行（其下合同由上层先删）。"""
        raise NotImplementedError

    # ---------- 合同（contract_files） ----------
    def add_contract(self, user_id, name, store_name, dir_name="uploads",
                     size=0, folder_id=None, note=""):
        """登记一份合同；store_name 冲突返回 None，否则返回新行 dict。"""
        raise NotImplementedError

    def list_contracts(self, user_id, folder_id=None):
        """返回该用户合同清单（可按文件夹过滤），含 folder_name。"""
        raise NotImplementedError

    def get_contract(self, user_id, contract_id):
        raise NotImplementedError

    def get_contract_by_store(self, user_id, store_name):
        raise NotImplementedError

    def find_contracts_by_title(self, user_id, name):
        """按标题（可重名）找合同，返回列表（按时间倒序）。"""
        raise NotImplementedError

    def update_contract(self, user_id, contract_id, *, name=None,
                        note=None, folder_id=None) -> bool:
        """更新合同的 标题/备注/所属文件夹。"""
        raise NotImplementedError

    def delete_contract(self, user_id, contract_id) -> bool:
        raise NotImplementedError

    # ---------- 兼容旧接口 ----------
    def add_file(self, user_id, name, dir_name="uploads", size=0) -> bool:
        """旧接口：登记到默认文件夹（无则建），store_name=name。"""
        raise NotImplementedError

    def list_files(self, user_id):
        """旧接口：= list_contracts(user_id)，字段向后兼容。"""
        raise NotImplementedError

    def get_file(self, user_id, name):
        """旧接口：按标题取最新一条（同名取最新）。"""
        raise NotImplementedError

    def delete_file(self, user_id, name) -> bool:
        """旧接口：删除该标题全部记录。"""
        raise NotImplementedError

    def move_files_to(self, usernames, user_id) -> int:
        """旧接口：批量登记到默认文件夹。"""
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
CREATE TABLE IF NOT EXISTS folders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT,
  UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS contract_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  store_name TEXT,
  dir TEXT DEFAULT 'uploads',
  size INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  folder_id INTEGER,
  created_at TEXT,
  UNIQUE(user_id, store_name)
);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""


def _sqlite_columns(conn, table):
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return {r["name"] for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        return set()


class SqliteStore(Store):
    def __init__(self, path=DB_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    def init_schema(self):
        with self._lock:
            self._conn.executescript(_SQLITE_SCHEMA)
            self._conn.commit()

    # ---------- 迁移：v1(无 folders/note/store_name/folder_id，user+name 唯一) → v2 ----------
    def migrate(self):
        with self._lock:
            if self.get_meta(_SCHEMA_VERSION_KEY) == _SCHEMA_VERSION:
                return
            cols = _sqlite_columns(self._conn, "contract_files")
            need = {"note", "store_name", "folder_id"}
            has_folders = "folders" in {
                r["name"] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            if not has_folders or not need.issubset(cols):
                # 旧结构 → 整表重建（保留数据，标题可重名）
                self._conn.executescript("""
                  CREATE TABLE IF NOT EXISTS folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT,
                    UNIQUE(user_id, name)
                  );
                  CREATE TABLE contract_files_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    store_name TEXT,
                    dir TEXT DEFAULT 'uploads',
                    size INTEGER DEFAULT 0,
                    note TEXT DEFAULT '',
                    folder_id INTEGER,
                    created_at TEXT,
                    UNIQUE(user_id, store_name)
                  );
                """)
                # 旧列名集合（可能缺 store_name 等，用 COALESCE 兜底）
                self._conn.execute("""
                  INSERT INTO contract_files_v2
                    (id,user_id,name,store_name,dir,size,note,folder_id,created_at)
                  SELECT id,user_id,name,name,dir,size,'',NULL,created_at
                  FROM contract_files
                """)
                self._conn.execute("DROP TABLE contract_files")
                self._conn.execute(
                    "ALTER TABLE contract_files_v2 RENAME TO contract_files")
            self._conn.commit()
            # 每个用户默认文件夹 + 回填 NULL folder_id
            self._ensure_all_default_folders()
            self.set_meta(_SCHEMA_VERSION_KEY, _SCHEMA_VERSION)
            self._conn.commit()

    def _ensure_all_default_folders(self):
        # 为每个用户建默认文件夹（忽略已存在）
        self._conn.execute(
            "INSERT OR IGNORE INTO folders(user_id,name,created_at) "
            "SELECT id,?,? FROM users", (DEFAULT_FOLDER_NAME, _now()))
        # 回填 folder_id 为空的合同到其用户的默认文件夹
        self._conn.execute(
            "UPDATE contract_files SET folder_id = "
            "(SELECT id FROM folders f WHERE f.user_id=contract_files.user_id "
            " AND f.name=?) WHERE folder_id IS NULL", (DEFAULT_FOLDER_NAME,))
        self._conn.commit()

    def _row(self, r):
        return dict(r) if r is not None else None

    # ---------- 用户 ----------
    def create_user(self, username, password_hash, display_name=""):
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO users(username,password_hash,display_name,created_at) "
                "VALUES(?,?,?,?)",
                (username, password_hash, display_name, _now()),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return -1
            self.ensure_default_folder(cur.lastrowid)
            return cur.lastrowid

    def get_user_by_name(self, username):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM users WHERE username=?", (username,))
            return self._row(cur.fetchone())

    def get_user_by_id(self, user_id):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
            return self._row(cur.fetchone())

    def list_users(self):
        with self._lock:
            cur = self._conn.execute("SELECT id,username FROM users ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    def create_session(self, token, user_id):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                (token, user_id, _now()))
            self._conn.commit()

    def delete_session(self, token):
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            self._conn.commit()

    def get_session_user(self, token):
        with self._lock:
            cur = self._conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                (token,))
            return self._row(cur.fetchone())

    # ---------- 文件夹 ----------
    def create_folder(self, user_id, name):
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO folders(user_id,name,created_at) VALUES(?,?,?)",
                (user_id, name, _now()))
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            cur = self._conn.execute(
                "SELECT * FROM folders WHERE user_id=? AND name=?", (user_id, name))
            return self._row(cur.fetchone())

    def get_folder(self, user_id, folder_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM folders WHERE user_id=? AND id=?", (user_id, folder_id))
            return self._row(cur.fetchone())

    def list_folders(self, user_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT f.id,f.name,f.created_at,"
                "(SELECT COUNT(*) FROM contract_files c WHERE c.folder_id=f.id) AS cnt "
                "FROM folders f WHERE f.user_id=? ORDER BY f.created_at, f.id",
                (user_id,))
            return [dict(r) for r in cur.fetchall()]

    def ensure_default_folder(self, user_id):
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO folders(user_id,name,created_at) VALUES(?,?,?)",
                (user_id, DEFAULT_FOLDER_NAME, _now()))
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT * FROM folders WHERE user_id=? AND name=?", (user_id, DEFAULT_FOLDER_NAME))
            return self._row(cur.fetchone())

    def delete_folder_row(self, user_id, folder_id):
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM folders WHERE user_id=? AND id=?", (user_id, folder_id))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- 合同 ----------
    def add_contract(self, user_id, name, store_name, dir_name="uploads",
                     size=0, folder_id=None, note=""):
        if not store_name:
            store_name = name
        if folder_id is None:
            folder_id = self.ensure_default_folder(user_id)["id"]
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO contract_files"
                "(user_id,name,store_name,dir,size,note,folder_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (user_id, name, store_name, dir_name, size, note or "", folder_id, _now()))
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return self.get_contract(user_id, cur.lastrowid)

    def list_contracts(self, user_id, folder_id=None):
        sql = ("SELECT c.id,c.name,c.store_name,c.dir,c.size,c.note,c.folder_id,"
               "c.created_at,f.name AS folder_name FROM contract_files c "
               "LEFT JOIN folders f ON f.id=c.folder_id WHERE c.user_id=? ")
        args = [user_id]
        if folder_id is not None:
            sql += "AND c.folder_id=? "
            args.append(folder_id)
        sql += "ORDER BY c.created_at DESC, c.id DESC"
        with self._lock:
            cur = self._conn.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def get_contract(self, user_id, contract_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT c.*,f.name AS folder_name FROM contract_files c "
                "LEFT JOIN folders f ON f.id=c.folder_id "
                "WHERE c.user_id=? AND c.id=?", (user_id, contract_id))
            return self._row(cur.fetchone())

    def get_contract_by_store(self, user_id, store_name):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM contract_files WHERE user_id=? AND store_name=?",
                (user_id, store_name))
            return self._row(cur.fetchone())

    def find_contracts_by_title(self, user_id, name):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM contract_files WHERE user_id=? AND name=? "
                "ORDER BY created_at DESC, id DESC", (user_id, name))
            return [dict(r) for r in cur.fetchall()]

    def update_contract(self, user_id, contract_id, *, name=None,
                        note=None, folder_id=None):
        sets, args = [], []
        if name is not None:
            sets.append("name=?"); args.append(name)
        if note is not None:
            sets.append("note=?"); args.append(note)
        if folder_id is not None:
            sets.append("folder_id=?"); args.append(folder_id)
        if not sets:
            return False
        args += [user_id, contract_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE contract_files SET {', '.join(sets)} "
                "WHERE user_id=? AND id=?", args)
            self._conn.commit()
            return cur.rowcount > 0

    def delete_contract(self, user_id, contract_id):
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM contract_files WHERE user_id=? AND id=?",
                (user_id, contract_id))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- 兼容旧接口 ----------
    def add_file(self, user_id, name, dir_name="uploads", size=0):
        return self.add_contract(user_id, name, name, dir_name, size) is not None

    def list_files(self, user_id):
        return self.list_contracts(user_id)

    def get_file(self, user_id, name):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM contract_files WHERE user_id=? AND name=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (user_id, name))
            return self._row(cur.fetchone())

    def delete_file(self, user_id, name):
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM contract_files WHERE user_id=? AND name=?", (user_id, name))
            self._conn.commit()
            return cur.rowcount > 0

    def move_files_to(self, usernames, user_id):
        added = 0
        for name in usernames:
            if self.add_file(user_id, name, "uploads", 0):
                added += 1
        return added

    def set_meta(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, str(value)))
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
CREATE TABLE IF NOT EXISTS folders (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NOT NULL,
  name VARCHAR(64) NOT NULL,
  created_at VARCHAR(32),
  UNIQUE KEY uk_user_folder (user_id, name)
) DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS contract_files (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NOT NULL,
  name VARCHAR(255) NOT NULL,
  store_name VARCHAR(255),
  dir VARCHAR(32) DEFAULT 'uploads',
  size INT DEFAULT 0,
  note TEXT,
  folder_id INT,
  created_at VARCHAR(32),
  UNIQUE KEY uk_user_store (user_id, store_name)
) DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS meta (
  k VARCHAR(64) PRIMARY KEY,
  v VARCHAR(255)
) DEFAULT CHARSET=utf8mb4;
"""


def _mysql_columns(conn, table):
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM `{table}`")
            return {r["Field"] for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        return set()


class MysqlStore(Store):
    def __init__(self, host=DB_HOST, port=DB_PORT, user=DB_USER,
                 password=DB_PASSWORD, db=DB_NAME):
        import pymysql  # 延迟导入：未安装时 SQLite 不受影响
        self._pymysql = pymysql
        self._cfg = dict(host=host, port=port, user=user, password=password,
                         charset="utf8mb4")
        self._db = db
        conn = self._connect_raw()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db}` DEFAULT CHARACTER SET utf8mb4")
            conn.commit()
        finally:
            conn.close()

    def _connect_raw(self):
        return self._pymysql.connect(
            **self._cfg, autocommit=True,
            cursorclass=self._pymysql.cursors.DictCursor)

    def _connect_db(self, db=None):
        return self._pymysql.connect(
            **self._cfg, database=db or self._db, autocommit=True,
            cursorclass=self._pymysql.cursors.DictCursor)

    def _q(self, sql, args=()):
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

    # ---------- 迁移 v1→v2 ----------
    def migrate(self):
        if self.get_meta(_SCHEMA_VERSION_KEY) == _SCHEMA_VERSION:
            return
        conn = self._connect_db()
        try:
            with conn.cursor() as cur:
                # 1) 建 folders 表（幂等）
                cur.execute("""CREATE TABLE IF NOT EXISTS folders (
                  id INT AUTO_INCREMENT PRIMARY KEY,
                  user_id INT NOT NULL,
                  name VARCHAR(64) NOT NULL,
                  created_at VARCHAR(32),
                  UNIQUE KEY uk_user_folder (user_id, name)
                ) DEFAULT CHARSET=utf8mb4""")
                # 2) contract_files 补列
                cols = set()
                cur.execute("SHOW COLUMNS FROM contract_files")
                cols = {r["Field"] for r in cur.fetchall()}
                if "note" not in cols:
                    cur.execute("ALTER TABLE contract_files ADD COLUMN note TEXT")
                if "store_name" not in cols:
                    cur.execute(
                        "ALTER TABLE contract_files ADD COLUMN store_name VARCHAR(255)")
                    # 旧行回填 store_name=name
                    cur.execute("UPDATE contract_files SET store_name=name "
                                "WHERE store_name IS NULL OR store_name=''")
                if "folder_id" not in cols:
                    cur.execute("ALTER TABLE contract_files ADD COLUMN folder_id INT")
                # 3) 去掉旧的 user+name 唯一索引（放开标题重名），改 user+store_name
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.statistics "
                            "WHERE table_schema=DATABASE() AND table_name='contract_files' "
                            "AND index_name='uk_user_file'")
                row = cur.fetchone()
                if row and row["n"]:
                    cur.execute("ALTER TABLE contract_files DROP INDEX uk_user_file")
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.statistics "
                            "WHERE table_schema=DATABASE() AND table_name='contract_files' "
                            "AND index_name='uk_user_store'")
                row = cur.fetchone()
                if not (row and row["n"]):
                    # MySQL 不支持 CREATE INDEX IF NOT EXISTS，先查再建
                    cur.execute(
                        "CREATE UNIQUE INDEX uk_user_store "
                        "ON contract_files(user_id, store_name)")
            conn.commit()
        finally:
            conn.close()
        # 4) 默认文件夹 + 回填
        self._ensure_all_default_folders()
        self.set_meta(_SCHEMA_VERSION_KEY, _SCHEMA_VERSION)

    def _ensure_all_default_folders(self):
        conn = self._connect_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT IGNORE INTO folders(user_id,name,created_at) "
                    "SELECT id,%s,%s FROM users", (DEFAULT_FOLDER_NAME, _now()))
                cur.execute(
                    "UPDATE contract_files c SET folder_id = "
                    "(SELECT id FROM folders f WHERE f.user_id=c.user_id "
                    " AND f.name=%s) WHERE c.folder_id IS NULL",
                    (DEFAULT_FOLDER_NAME,))
            conn.commit()
        finally:
            conn.close()

    # ---------- 用户 ----------
    def create_user(self, username, password_hash, display_name=""):
        try:
            conn, n = self._q(
                "INSERT IGNORE INTO users(username,password_hash,display_name,created_at) "
                "VALUES(%s,%s,%s,%s)",
                (username, password_hash, display_name, _now()))
            conn.close()
            if not n:
                return -1
            r = self._fetch("SELECT id FROM users WHERE username=%s", (username,), one=True)
            uid = r["id"] if r else -1
            if uid > 0:
                self.ensure_default_folder(uid)
            return uid
        except Exception:  # noqa: BLE001
            return -1

    def get_user_by_name(self, username):
        return self._fetch("SELECT * FROM users WHERE username=%s", (username,), one=True)

    def get_user_by_id(self, user_id):
        return self._fetch("SELECT * FROM users WHERE id=%s", (user_id,), one=True)

    def list_users(self):
        return self._fetch("SELECT id,username FROM users ORDER BY id")

    def create_session(self, token, user_id):
        self._q("REPLACE INTO sessions(token,user_id,created_at) VALUES(%s,%s,%s)",
                (token, user_id, _now()))[0].close()

    def delete_session(self, token):
        self._q("DELETE FROM sessions WHERE token=%s", (token,))[0].close()

    def get_session_user(self, token):
        return self._fetch(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=%s",
            (token,), one=True)

    # ---------- 文件夹 ----------
    def create_folder(self, user_id, name):
        conn, n = self._q(
            "INSERT IGNORE INTO folders(user_id,name,created_at) VALUES(%s,%s,%s)",
            (user_id, name, _now()))
        conn.close()
        if not n:
            return None
        return self._fetch(
            "SELECT * FROM folders WHERE user_id=%s AND name=%s",
            (user_id, name), one=True)

    def get_folder(self, user_id, folder_id):
        return self._fetch(
            "SELECT * FROM folders WHERE user_id=%s AND id=%s", (user_id, folder_id), one=True)

    def list_folders(self, user_id):
        return self._fetch(
            "SELECT f.id,f.name,f.created_at,"
            "(SELECT COUNT(*) FROM contract_files c WHERE c.folder_id=f.id) AS cnt "
            "FROM folders f WHERE f.user_id=%s ORDER BY f.created_at, f.id", (user_id,))

    def ensure_default_folder(self, user_id):
        self._q(
            "INSERT IGNORE INTO folders(user_id,name,created_at) VALUES(%s,%s,%s)",
            (user_id, DEFAULT_FOLDER_NAME, _now()))[0].close()
        return self._fetch(
            "SELECT * FROM folders WHERE user_id=%s AND name=%s",
            (user_id, DEFAULT_FOLDER_NAME), one=True)

    def delete_folder_row(self, user_id, folder_id):
        conn, n = self._q(
            "DELETE FROM folders WHERE user_id=%s AND id=%s", (user_id, folder_id))
        conn.close()
        return n > 0

    # ---------- 合同 ----------
    def add_contract(self, user_id, name, store_name, dir_name="uploads",
                     size=0, folder_id=None, note=""):
        if not store_name:
            store_name = name
        if folder_id is None:
            folder_id = self.ensure_default_folder(user_id)["id"]
        try:
            conn, n = self._q(
                "INSERT IGNORE INTO contract_files"
                "(user_id,name,store_name,dir,size,note,folder_id,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, name, store_name, dir_name, size, note or "", folder_id, _now()))
            conn.close()
            if not n:
                return None
            return self._fetch(
                "SELECT c.*,f.name AS folder_name FROM contract_files c "
                "LEFT JOIN folders f ON f.id=c.folder_id "
                "WHERE c.user_id=%s AND c.store_name=%s",
                (user_id, store_name), one=True)
        except Exception:  # noqa: BLE001
            return None

    def list_contracts(self, user_id, folder_id=None):
        sql = ("SELECT c.id,c.name,c.store_name,c.dir,c.size,c.note,c.folder_id,"
               "c.created_at,f.name AS folder_name FROM contract_files c "
               "LEFT JOIN folders f ON f.id=c.folder_id WHERE c.user_id=%s ")
        args = [user_id]
        if folder_id is not None:
            sql += "AND c.folder_id=%s "
            args.append(folder_id)
        sql += "ORDER BY c.created_at DESC, c.id DESC"
        return self._fetch(sql, args)

    def get_contract(self, user_id, contract_id):
        return self._fetch(
            "SELECT c.*,f.name AS folder_name FROM contract_files c "
            "LEFT JOIN folders f ON f.id=c.folder_id "
            "WHERE c.user_id=%s AND c.id=%s", (user_id, contract_id), one=True)

    def get_contract_by_store(self, user_id, store_name):
        return self._fetch(
            "SELECT * FROM contract_files WHERE user_id=%s AND store_name=%s",
            (user_id, store_name), one=True)

    def find_contracts_by_title(self, user_id, name):
        return self._fetch(
            "SELECT * FROM contract_files WHERE user_id=%s AND name=%s "
            "ORDER BY created_at DESC, id DESC", (user_id, name))

    def update_contract(self, user_id, contract_id, *, name=None,
                        note=None, folder_id=None):
        sets, args = [], []
        if name is not None:
            sets.append("name=%s"); args.append(name)
        if note is not None:
            sets.append("note=%s"); args.append(note)
        if folder_id is not None:
            sets.append("folder_id=%s"); args.append(folder_id)
        if not sets:
            return False
        args += [user_id, contract_id]
        conn, n = self._q(
            f"UPDATE contract_files SET {', '.join(sets)} "
            "WHERE user_id=%s AND id=%s", args)
        conn.close()
        return n > 0

    def delete_contract(self, user_id, contract_id):
        conn, n = self._q(
            "DELETE FROM contract_files WHERE user_id=%s AND id=%s",
            (user_id, contract_id))
        conn.close()
        return n > 0

    # ---------- 兼容旧接口 ----------
    def add_file(self, user_id, name, dir_name="uploads", size=0):
        return self.add_contract(user_id, name, name, dir_name, size) is not None

    def list_files(self, user_id):
        return self.list_contracts(user_id)

    def get_file(self, user_id, name):
        return self._fetch(
            "SELECT * FROM contract_files WHERE user_id=%s AND name=%s "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (user_id, name), one=True)

    def delete_file(self, user_id, name):
        conn, n = self._q(
            "DELETE FROM contract_files WHERE user_id=%s AND name=%s", (user_id, name))
        conn.close()
        return n > 0

    def move_files_to(self, usernames, user_id):
        added = 0
        for name in usernames:
            if self.add_file(user_id, name, "uploads", 0):
                added += 1
        return added

    def set_meta(self, key, value):
        self._q("REPLACE INTO meta(k,v) VALUES(%s,%s)", (key, str(value)))[0].close()

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
            try:
                _store.migrate()
            except Exception as e:  # noqa: BLE001
                print(f"[storage] 迁移警告（可忽略，将重试）：{e}")
        return _store


def reset_store():
    """仅供测试：强制重建 store。"""
    global _store
    with _store_lock:
        _store = None
    return get_store()
