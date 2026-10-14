# -*- coding: utf-8 -*-
"""
storage.py —— 用户系统 / 合同库 / 文件夹分组 的统一存储层（v2）

同一套业务接口，两个后端实现：
  * SqliteStore：DB_ENGINE=sqlite（默认，本地文件 contract_ai.db，零配置）
  * MysqlStore ：DB_ENGINE=mysql（需在 .env 配置 DB_HOST/DB_USER/DB_PASSWORD/DB_NAME）

表结构（两后端一致）：
  users           用户（id/username/password_hash/display_name/created_at
                   + llm_provider/cloud_api_key/cloud_model —— 模型引擎偏好）
  sessions        token 会话
  folders         合同库文件夹（id/user_id/name/created_at，user+name 唯一）
  contract_files  合同记录（id/user_id/name[显示标题,可重名]/store_name[磁盘唯一名]
                   /dir/size/note[备注]/folder_id[所属文件夹]/created_at，
                   user+store_name 唯一 —— 允许同名标题各自独立成库）
  meta            键值（记录初始化/预置状态/迁移版本等）

v2 迁移：自动补列（note/store_name/folder_id）、建 folders、为既有用户建默认
文件夹“我的合同”并把旧合同回填进去、放开「标题唯一」约束（旧库为 user+name
唯一，改为 user+store_name 唯一）。
v3 迁移：users 表补 llm_provider / cloud_api_key / cloud_model 三列
（模型引擎偏好：本地 Ollama ⇄ 云端 API，可由用户在网页端自由切换）。
v5 迁移：review_findings 保存最近一次审查的结构化发现。新审查成功后覆盖旧行。
v6 迁移：users.role（admin/legal/viewer，旧用户默认 legal）与 audit_logs。
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
_SCHEMA_VERSION = "6"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """存储层统一接口（子类实现）。"""

    def init_schema(self):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def ping(self):  # pragma: no cover - 由子类实现
        """执行一次轻量查询，确认连接可用。失败则抛异常。"""
        raise NotImplementedError

    def migrate(self):  # pragma: no cover - 由子类实现
        """v1 → v2 数据迁移（建 folders、补列、回填默认文件夹）。"""
        raise NotImplementedError

    # ---------- 用户 ----------
    def create_user(self, username, password_hash, display_name="", role="legal") -> int:
        raise NotImplementedError

    def get_user_by_name(self, username):
        raise NotImplementedError

    def get_user_by_id(self, user_id):
        raise NotImplementedError

    def list_users(self):
        """枚举用户的公开字段（不含密码与密钥）。"""
        raise NotImplementedError

    def count_users(self) -> int:
        raise NotImplementedError

    def count_role(self, role: str) -> int:
        raise NotImplementedError

    def set_user_role(self, user_id, role: str) -> bool:
        """设置角色。未知角色抛 ValueError。"""
        raise NotImplementedError

    def write_audit(self, *, user_id=None, username="", action="",
                    target_type="", target_id="", detail=None, ip=""):
        """写审计。失败只打日志，不抛给业务。"""
        raise NotImplementedError

    def list_audit(self, action="", limit=100, offset=0):
        raise NotImplementedError

    # ---------- 用户偏好（模型引擎：本地 Ollama / 云端 API） ----------
    def update_user_llm(self, user_id, *, llm_provider=None,
                        cloud_api_key=None, cloud_model=None) -> bool:
        """更新用户的模型引擎偏好。None=不修改；传 ''=清空该字段。

        llm_provider : ''（跟随系统默认）/ 'local' / 'cloud'
        cloud_api_key: 用户在网页端填写的个人云端密钥（''=清除，改用 .env 共享密钥）
        cloud_model  : 个人云端模型名覆盖（''=清除，用 .env 的 CLOUD_MODEL）
        """
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

    def set_contract_status(self, user_id, contract_id, status, *,
                            job_id=None, error=None,
                            touch_job=False, touch_error=False) -> bool:
        """按状态机更新 status，并在状态变化时写 status_updated_at。

        touch_job / touch_error 为真时才改 current_job_id / last_error（可传 None 清空）。
        合同不存在返回 False；非法流转抛 IllegalTransition。
        """
        raise NotImplementedError

    def delete_contract(self, user_id, contract_id) -> bool:
        raise NotImplementedError

    # ---------- 审查发现（review_findings） ----------
    def replace_contract_findings(self, user_id, contract_id, job_id, rows) -> int:
        """覆盖该用户该合同的全部 findings。合同不存在返回 0。失败回滚。"""
        raise NotImplementedError

    def list_findings(self, user_id, contract_id, status=None):
        raise NotImplementedError

    def get_finding(self, user_id, finding_id):
        raise NotImplementedError

    def update_finding(self, user_id, finding_id, *, status=None, note=None):
        """更新状态或备注。记录不存在返回 None；状态非法抛 ValueError。"""
        raise NotImplementedError

    def findings_summary(self, user_id, contract_id):
        """合同不属于该用户时返回 None。"""
        raise NotImplementedError

    def open_finding_counts(self, user_id) -> dict:
        """{contract_id: 未处理条数}。"""
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
  role TEXT DEFAULT 'legal',
  llm_provider TEXT DEFAULT '',
  cloud_api_key TEXT DEFAULT '',
  cloud_model TEXT DEFAULT '',
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
  status TEXT DEFAULT 'ready',
  current_job_id TEXT,
  last_error TEXT,
  status_updated_at TEXT,
  created_at TEXT,
  UNIQUE(user_id, store_name)
);
CREATE TABLE IF NOT EXISTS review_findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  dimension TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  title TEXT NOT NULL DEFAULT '',
  detail TEXT DEFAULT '',
  quote TEXT DEFAULT '',
  location TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  note TEXT DEFAULT '',
  job_id TEXT DEFAULT '',
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_contract
  ON review_findings(user_id, contract_id);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  username TEXT DEFAULT '',
  action TEXT NOT NULL,
  target_type TEXT DEFAULT '',
  target_id TEXT DEFAULT '',
  detail TEXT DEFAULT '',
  ip TEXT DEFAULT '',
  created_at TEXT
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
        # 可重入：migrate/create_user 会在已持锁时再调用 get_meta、set_meta、ensure_default_folder。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    def init_schema(self):
        with self._lock:
            self._conn.executescript(_SQLITE_SCHEMA)
            self._conn.commit()

    def ping(self):
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

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
            # v3：users 表补「模型引擎偏好」列（本地/云端切换 + 个人密钥，幂等补列）
            ucols = _sqlite_columns(self._conn, "users")
            for _col, _ddl in (
                ("llm_provider", "TEXT DEFAULT ''"),
                ("cloud_api_key", "TEXT DEFAULT ''"),
                ("cloud_model", "TEXT DEFAULT ''"),
            ):
                if _col not in ucols:
                    self._conn.execute(
                        f"ALTER TABLE users ADD COLUMN {_col} {_ddl}")
            self._conn.commit()
            # 每个用户默认文件夹 + 回填 NULL folder_id
            self._ensure_all_default_folders()
            self._ensure_contract_status()
            self._ensure_review_findings()
            self._ensure_user_role()
            self._ensure_audit_logs()
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

    def _ensure_contract_status(self):
        """v4：补 status / current_job_id / last_error / status_updated_at。

        旧行没有这些列时默认 status=ready（视为已可审查），不把历史合同打回待入库。
        """
        cols = _sqlite_columns(self._conn, "contract_files")
        added = "status" not in cols
        if "status" not in cols:
            self._conn.execute(
                "ALTER TABLE contract_files ADD COLUMN status TEXT DEFAULT 'ready'")
        if "current_job_id" not in cols:
            self._conn.execute(
                "ALTER TABLE contract_files ADD COLUMN current_job_id TEXT")
        if "last_error" not in cols:
            self._conn.execute(
                "ALTER TABLE contract_files ADD COLUMN last_error TEXT")
        if "status_updated_at" not in cols:
            self._conn.execute(
                "ALTER TABLE contract_files ADD COLUMN status_updated_at TEXT")
        self._conn.execute(
            "UPDATE contract_files SET status='ready' WHERE status IS NULL OR status=''")
        self._conn.execute(
            "UPDATE contract_files SET status_updated_at=created_at "
            "WHERE status_updated_at IS NULL OR status_updated_at=''")
        self._conn.commit()
        if added:
            print("[storage] 已补齐合同状态字段，旧合同默认 status=ready", flush=True)

    def _ensure_review_findings(self):
        """v5：审查发现表。已有库补表，不改动合同行。"""
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='review_findings'")
        missing = cur.fetchone() is None
        if missing:
            self._conn.executescript("""
              CREATE TABLE review_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                dimension TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                title TEXT NOT NULL DEFAULT '',
                detail TEXT DEFAULT '',
                quote TEXT DEFAULT '',
                location TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                note TEXT DEFAULT '',
                job_id TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
              );
              CREATE INDEX IF NOT EXISTS idx_findings_contract
                ON review_findings(user_id, contract_id);
            """)
            print("[storage] 已创建 review_findings，保存最近一次审查发现", flush=True)
        self._conn.commit()

    def _ensure_user_role(self):
        """v6：用户角色。旧用户默认 legal。"""
        cols = _sqlite_columns(self._conn, "users")
        if "role" not in cols:
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'legal'")
            print("[storage] 已补齐 users.role，旧用户默认 legal", flush=True)
        self._conn.execute(
            "UPDATE users SET role='legal' WHERE role IS NULL OR role=''")
        self._conn.commit()

    def _ensure_audit_logs(self):
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_logs'")
        if cur.fetchone() is None:
            self._conn.executescript("""
              CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT DEFAULT '',
                action TEXT NOT NULL,
                target_type TEXT DEFAULT '',
                target_id TEXT DEFAULT '',
                detail TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                created_at TEXT
              );
            """)
            print("[storage] 已创建 audit_logs", flush=True)
        self._conn.commit()

    def _row(self, r):
        return dict(r) if r is not None else None

    # ---------- 用户 ----------
    def create_user(self, username, password_hash, display_name="", role="legal"):
        if role not in ("admin", "legal", "viewer"):
            role = "legal"
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO users(username,password_hash,display_name,role,created_at) "
                "VALUES(?,?,?,?,?)",
                (username, password_hash, display_name, role, _now()),
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
            cur = self._conn.execute(
                "SELECT id,username,display_name,role,created_at FROM users ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    def count_users(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM users")
            return int(cur.fetchone()["n"])

    def count_role(self, role: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role=?", (role,))
            return int(cur.fetchone()["n"])

    def set_user_role(self, user_id, role: str) -> bool:
        if role not in ("admin", "legal", "viewer"):
            raise ValueError("未知角色")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET role=? WHERE id=?", (role, user_id))
            self._conn.commit()
            return cur.rowcount > 0

    def write_audit(self, *, user_id=None, username="", action="",
                    target_type="", target_id="", detail=None, ip=""):
        import json
        try:
            if isinstance(detail, dict):
                banned = {"password", "api_key", "cloud_api_key", "token", "authorization"}
                safe = {k: v for k, v in detail.items() if str(k).lower() not in banned}
                text = json.dumps(safe, ensure_ascii=False)
            else:
                text = "" if detail is None else str(detail)
            if len(text) > 1000:
                text = text[:999] + "…"
            with self._lock:
                self._conn.execute(
                    "INSERT INTO audit_logs(user_id,username,action,target_type,"
                    "target_id,detail,ip,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (user_id, (username or "")[:64], (action or "")[:64],
                     (target_type or "")[:32], str(target_id or "")[:64],
                     text, (ip or "")[:64], _now()))
                self._conn.commit()
        except Exception as e:  # noqa: BLE001
            print(f"[audit] 写入失败：{e}", flush=True)

    def list_audit(self, action="", limit=100, offset=0):
        sql = "SELECT * FROM audit_logs "
        args = []
        if action:
            sql += "WHERE action=? "
            args.append(action)
        sql += "ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([max(1, int(limit)), max(0, int(offset))])
        with self._lock:
            cur = self._conn.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def update_user_llm(self, user_id, *, llm_provider=None,
                        cloud_api_key=None, cloud_model=None):
        sets, args = [], []
        if llm_provider is not None:
            sets.append("llm_provider=?"); args.append(llm_provider)
        if cloud_api_key is not None:
            sets.append("cloud_api_key=?"); args.append(cloud_api_key)
        if cloud_model is not None:
            sets.append("cloud_model=?"); args.append(cloud_model)
        if not sets:
            return False
        args.append(user_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)
            self._conn.commit()
            return cur.rowcount > 0

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
                     size=0, folder_id=None, note="", status="uploaded"):
        if not store_name:
            store_name = name
        if folder_id is None:
            folder_id = self.ensure_default_folder(user_id)["id"]
        if status not in ("uploaded", "ingesting", "ready", "reviewing",
                          "reviewed", "archived", "failed"):
            status = "uploaded"
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO contract_files"
                "(user_id,name,store_name,dir,size,note,folder_id,status,"
                "status_updated_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (user_id, name, store_name, dir_name, size, note or "",
                 folder_id, status, now, now))
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return self.get_contract(user_id, cur.lastrowid)

    def list_contracts(self, user_id, folder_id=None, status=None, q=None,
                       limit=None, offset=0):
        sql = ("SELECT c.id,c.name,c.store_name,c.dir,c.size,c.note,c.folder_id,"
               "c.status,c.current_job_id,c.last_error,c.status_updated_at,"
               "c.created_at,f.name AS folder_name FROM contract_files c "
               "LEFT JOIN folders f ON f.id=c.folder_id WHERE c.user_id=? ")
        args = [user_id]
        if folder_id is not None:
            sql += "AND c.folder_id=? "
            args.append(folder_id)
        if status:
            sql += "AND c.status=? "
            args.append(status)
        if q:
            like = "%" + q.replace("%", "") + "%"
            sql += "AND (c.name LIKE ? OR IFNULL(c.store_name,'') LIKE ?) "
            args.extend([like, like])
        sql += "ORDER BY c.created_at DESC, c.id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args.extend([int(limit), int(offset or 0)])
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

    def set_contract_status(self, user_id, contract_id, status, *,
                            job_id=None, error=None,
                            touch_job=False, touch_error=False) -> bool:
        from store.contract_status import STATUSES, can_transition, IllegalTransition
        if status not in STATUSES:
            raise IllegalTransition(f"未知状态：{status}")
        row = self.get_contract(user_id, contract_id)
        if not row:
            return False
        src = row.get("status") or "ready"
        if not can_transition(src, status):
            raise IllegalTransition(f"不允许从 {src} 变为 {status}")
        sets = ["status=?", "status_updated_at=?"]
        args = [status, _now()]
        if touch_job:
            sets.append("current_job_id=?")
            args.append(job_id or None)
        if touch_error:
            text = error or ""
            if len(text) > 500:
                text = text[:499] + "…"
            sets.append("last_error=?")
            args.append(text)
        args += [user_id, contract_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE contract_files SET {', '.join(sets)} "
                "WHERE user_id=? AND id=?", args)
            self._conn.commit()
            return cur.rowcount > 0

    def delete_contract(self, user_id, contract_id):
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM review_findings WHERE user_id=? AND contract_id=?",
                    (user_id, contract_id))
                cur = self._conn.execute(
                    "DELETE FROM contract_files WHERE user_id=? AND id=?",
                    (user_id, contract_id))
                self._conn.commit()
                return cur.rowcount > 0
            except Exception:
                self._conn.rollback()
                raise

    def replace_contract_findings(self, user_id, contract_id, job_id, rows) -> int:
        from store.findings import merge_prior_decisions, normalize_row
        if not self.get_contract(user_id, contract_id):
            return 0
        prior = self.list_findings(user_id, contract_id)
        clean = merge_prior_decisions([normalize_row(r) for r in (rows or [])], prior)
        now = _now()
        jid = (job_id or "")[:64]
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM review_findings WHERE user_id=? AND contract_id=?",
                    (user_id, contract_id))
                for r in clean:
                    self._conn.execute(
                        "INSERT INTO review_findings("
                        "contract_id,user_id,dimension,severity,title,detail,quote,"
                        "location,status,note,job_id,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (contract_id, user_id, r["dimension"], r["severity"], r["title"],
                         r["detail"], r["quote"], r["location"],
                         r.get("status") or "open", r.get("note") or "",
                         jid, now, now))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return len(clean)

    def list_findings(self, user_id, contract_id, status=None):
        sql = ("SELECT * FROM review_findings WHERE user_id=? AND contract_id=? ")
        args = [user_id, contract_id]
        if status:
            sql += "AND status=? "
            args.append(status)
        sql += "ORDER BY id ASC"
        with self._lock:
            cur = self._conn.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def get_finding(self, user_id, finding_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM review_findings WHERE user_id=? AND id=?",
                (user_id, finding_id))
            return self._row(cur.fetchone())

    def update_finding(self, user_id, finding_id, *, status=None, note=None):
        from store.findings import STATUSES
        row = self.get_finding(user_id, finding_id)
        if not row:
            return None
        if status is None and note is None:
            return row
        if status is not None and status not in STATUSES:
            raise ValueError("未知处理状态")
        new_status = status if status is not None else row.get("status") or "open"
        new_note = note if note is not None else (row.get("note") or "")
        if len(new_note) > 500:
            new_note = new_note[:499] + "…"
        with self._lock:
            self._conn.execute(
                "UPDATE review_findings SET status=?, note=?, updated_at=? "
                "WHERE user_id=? AND id=?",
                (new_status, new_note, _now(), user_id, finding_id))
            self._conn.commit()
        return self.get_finding(user_id, finding_id)

    def findings_summary(self, user_id, contract_id):
        from store.findings import SEVERITIES, STATUSES
        if not self.get_contract(user_id, contract_id):
            return None
        by_sev = {k: 0 for k in SEVERITIES}
        by_st = {k: 0 for k in STATUSES}
        with self._lock:
            cur = self._conn.execute(
                "SELECT severity, status, COUNT(*) AS n FROM review_findings "
                "WHERE user_id=? AND contract_id=? GROUP BY severity, status",
                (user_id, contract_id))
            grouped = [dict(r) for r in cur.fetchall()]
        total = 0
        for r in grouped:
            n = int(r.get("n") or 0)
            total += n
            if r.get("severity") in by_sev:
                by_sev[r["severity"]] += n
            if r.get("status") in by_st:
                by_st[r["status"]] += n
        return {
            "contract_id": contract_id,
            "total": total,
            "open": by_st.get("open", 0),
            "by_severity": by_sev,
            "by_status": by_st,
        }

    def open_finding_counts(self, user_id) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT contract_id, COUNT(*) AS n FROM review_findings "
                "WHERE user_id=? AND status='open' GROUP BY contract_id",
                (user_id,))
            return {int(r["contract_id"]): int(r["n"]) for r in cur.fetchall()}

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
            try:
                ids = [r["id"] for r in self._conn.execute(
                    "SELECT id FROM contract_files WHERE user_id=? AND name=?",
                    (user_id, name))]
                for cid in ids:
                    self._conn.execute(
                        "DELETE FROM review_findings WHERE user_id=? AND contract_id=?",
                        (user_id, cid))
                cur = self._conn.execute(
                    "DELETE FROM contract_files WHERE user_id=? AND name=?",
                    (user_id, name))
                self._conn.commit()
                return cur.rowcount > 0
            except Exception:
                self._conn.rollback()
                raise

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
  role VARCHAR(16) DEFAULT 'legal',
  llm_provider VARCHAR(16) DEFAULT '',
  cloud_api_key VARCHAR(255) DEFAULT '',
  cloud_model VARCHAR(128) DEFAULT '',
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
  status VARCHAR(32) DEFAULT 'ready',
  current_job_id VARCHAR(64),
  last_error TEXT,
  status_updated_at VARCHAR(32),
  created_at VARCHAR(32),
  UNIQUE KEY uk_user_store (user_id, store_name)
) DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS review_findings (
  id INT AUTO_INCREMENT PRIMARY KEY,
  contract_id INT NOT NULL,
  user_id INT NOT NULL,
  dimension VARCHAR(64) NOT NULL,
  severity VARCHAR(16) NOT NULL DEFAULT 'info',
  title VARCHAR(200) NOT NULL DEFAULT '',
  detail TEXT,
  quote TEXT,
  location TEXT,
  status VARCHAR(16) NOT NULL DEFAULT 'open',
  note TEXT,
  job_id VARCHAR(64) DEFAULT '',
  created_at VARCHAR(32),
  updated_at VARCHAR(32),
  KEY idx_findings_contract (user_id, contract_id)
) DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS audit_logs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NULL,
  username VARCHAR(64) DEFAULT '',
  action VARCHAR(64) NOT NULL,
  target_type VARCHAR(32) DEFAULT '',
  target_id VARCHAR(64) DEFAULT '',
  detail TEXT,
  ip VARCHAR(64) DEFAULT '',
  created_at VARCHAR(32),
  KEY idx_audit_action (action)
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

    def ping(self):
        conn, _n = self._q("SELECT 1")
        conn.close()

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
                # v3：users 表补「模型引擎偏好」列（本地/云端切换 + 个人密钥，幂等补列）
                cur.execute("SHOW COLUMNS FROM users")
                ucols = {r["Field"] for r in cur.fetchall()}
                if "llm_provider" not in ucols:
                    cur.execute("ALTER TABLE users ADD COLUMN "
                                "llm_provider VARCHAR(16) DEFAULT ''")
                if "cloud_api_key" not in ucols:
                    cur.execute("ALTER TABLE users ADD COLUMN "
                                "cloud_api_key VARCHAR(255) DEFAULT ''")
                if "cloud_model" not in ucols:
                    cur.execute("ALTER TABLE users ADD COLUMN "
                                "cloud_model VARCHAR(128) DEFAULT ''")
            conn.commit()
        finally:
            conn.close()
        # 4) 默认文件夹 + 回填
        self._ensure_all_default_folders()
        self._ensure_contract_status()
        self._ensure_review_findings()
        self._ensure_user_role()
        self._ensure_audit_logs()
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

    def _ensure_contract_status(self):
        """v4：补合同生命周期列。旧行默认 status=ready。"""
        conn = self._connect_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW COLUMNS FROM contract_files")
                cols = {r["Field"] for r in cur.fetchall()}
                added = "status" not in cols
                if "status" not in cols:
                    cur.execute("ALTER TABLE contract_files ADD COLUMN "
                                "status VARCHAR(32) DEFAULT 'ready'")
                if "current_job_id" not in cols:
                    cur.execute("ALTER TABLE contract_files ADD COLUMN "
                                "current_job_id VARCHAR(64) NULL")
                if "last_error" not in cols:
                    cur.execute("ALTER TABLE contract_files ADD COLUMN last_error TEXT")
                if "status_updated_at" not in cols:
                    cur.execute("ALTER TABLE contract_files ADD COLUMN "
                                "status_updated_at VARCHAR(32) NULL")
                cur.execute("UPDATE contract_files SET status='ready' "
                            "WHERE status IS NULL OR status=''")
                cur.execute("UPDATE contract_files SET status_updated_at=created_at "
                            "WHERE status_updated_at IS NULL OR status_updated_at=''")
            conn.commit()
        finally:
            conn.close()
        if added:
            print("[storage] 已补齐合同状态字段，旧合同默认 status=ready", flush=True)

    def _ensure_review_findings(self):
        """v5：审查发现表。已有库补表，不改动合同行。"""
        conn = self._connect_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES LIKE 'review_findings'")
                missing = cur.fetchone() is None
                if missing:
                    cur.execute("""
                      CREATE TABLE review_findings (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        contract_id INT NOT NULL,
                        user_id INT NOT NULL,
                        dimension VARCHAR(64) NOT NULL,
                        severity VARCHAR(16) NOT NULL DEFAULT 'info',
                        title VARCHAR(200) NOT NULL DEFAULT '',
                        detail TEXT,
                        quote TEXT,
                        location TEXT,
                        status VARCHAR(16) NOT NULL DEFAULT 'open',
                        note TEXT,
                        job_id VARCHAR(64) DEFAULT '',
                        created_at VARCHAR(32),
                        updated_at VARCHAR(32),
                        KEY idx_findings_contract (user_id, contract_id)
                      ) DEFAULT CHARSET=utf8mb4
                    """)
            conn.commit()
        finally:
            conn.close()
        if missing:
            print("[storage] 已创建 review_findings，保存最近一次审查发现", flush=True)

    def _ensure_user_role(self):
        """v6：用户角色。旧用户默认 legal。"""
        conn = self._connect_db()
        added = False
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW COLUMNS FROM users")
                cols = {r["Field"] for r in cur.fetchall()}
                if "role" not in cols:
                    cur.execute(
                        "ALTER TABLE users ADD COLUMN role VARCHAR(16) DEFAULT 'legal'")
                    added = True
                cur.execute(
                    "UPDATE users SET role='legal' WHERE role IS NULL OR role=''")
            conn.commit()
        finally:
            conn.close()
        if added:
            print("[storage] 已补齐 users.role，旧用户默认 legal", flush=True)

    def _ensure_audit_logs(self):
        conn = self._connect_db()
        missing = False
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES LIKE 'audit_logs'")
                missing = cur.fetchone() is None
                if missing:
                    cur.execute("""
                      CREATE TABLE audit_logs (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NULL,
                        username VARCHAR(64) DEFAULT '',
                        action VARCHAR(64) NOT NULL,
                        target_type VARCHAR(32) DEFAULT '',
                        target_id VARCHAR(64) DEFAULT '',
                        detail TEXT,
                        ip VARCHAR(64) DEFAULT '',
                        created_at VARCHAR(32),
                        KEY idx_audit_action (action)
                      ) DEFAULT CHARSET=utf8mb4
                    """)
            conn.commit()
        finally:
            conn.close()
        if missing:
            print("[storage] 已创建 audit_logs", flush=True)

    # ---------- 用户 ----------
    def create_user(self, username, password_hash, display_name="", role="legal"):
        if role not in ("admin", "legal", "viewer"):
            role = "legal"
        try:
            conn, n = self._q(
                "INSERT IGNORE INTO users(username,password_hash,display_name,role,created_at) "
                "VALUES(%s,%s,%s,%s,%s)",
                (username, password_hash, display_name, role, _now()))
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
        return self._fetch(
            "SELECT id,username,display_name,role,created_at FROM users ORDER BY id") or []

    def count_users(self) -> int:
        row = self._fetch("SELECT COUNT(*) AS n FROM users", one=True) or {}
        return int(row.get("n") or 0)

    def count_role(self, role: str) -> int:
        row = self._fetch(
            "SELECT COUNT(*) AS n FROM users WHERE role=%s", (role,), one=True) or {}
        return int(row.get("n") or 0)

    def set_user_role(self, user_id, role: str) -> bool:
        if role not in ("admin", "legal", "viewer"):
            raise ValueError("未知角色")
        conn, n = self._q("UPDATE users SET role=%s WHERE id=%s", (role, user_id))
        conn.close()
        return n > 0

    def write_audit(self, *, user_id=None, username="", action="",
                    target_type="", target_id="", detail=None, ip=""):
        import json
        try:
            if isinstance(detail, dict):
                banned = {"password", "api_key", "cloud_api_key", "token", "authorization"}
                safe = {k: v for k, v in detail.items() if str(k).lower() not in banned}
                text = json.dumps(safe, ensure_ascii=False)
            else:
                text = "" if detail is None else str(detail)
            if len(text) > 1000:
                text = text[:999] + "…"
            conn, _n = self._q(
                "INSERT INTO audit_logs(user_id,username,action,target_type,"
                "target_id,detail,ip,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, (username or "")[:64], (action or "")[:64],
                 (target_type or "")[:32], str(target_id or "")[:64],
                 text, (ip or "")[:64], _now()))
            conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"[audit] 写入失败：{e}", flush=True)

    def list_audit(self, action="", limit=100, offset=0):
        sql = "SELECT * FROM audit_logs "
        args = []
        if action:
            sql += "WHERE action=%s "
            args.append(action)
        sql += "ORDER BY id DESC LIMIT %s OFFSET %s"
        args.extend([max(1, int(limit)), max(0, int(offset))])
        return self._fetch(sql, args) or []

    def update_user_llm(self, user_id, *, llm_provider=None,
                        cloud_api_key=None, cloud_model=None):
        sets, args = [], []
        if llm_provider is not None:
            sets.append("llm_provider=%s"); args.append(llm_provider)
        if cloud_api_key is not None:
            sets.append("cloud_api_key=%s"); args.append(cloud_api_key)
        if cloud_model is not None:
            sets.append("cloud_model=%s"); args.append(cloud_model)
        if not sets:
            return False
        args.append(user_id)
        conn, n = self._q(
            f"UPDATE users SET {', '.join(sets)} WHERE id=%s", args)
        conn.close()
        return n > 0

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
                     size=0, folder_id=None, note="", status="uploaded"):
        if not store_name:
            store_name = name
        if folder_id is None:
            folder_id = self.ensure_default_folder(user_id)["id"]
        if status not in ("uploaded", "ingesting", "ready", "reviewing",
                          "reviewed", "archived", "failed"):
            status = "uploaded"
        now = _now()
        try:
            conn, n = self._q(
                "INSERT IGNORE INTO contract_files"
                "(user_id,name,store_name,dir,size,note,folder_id,status,"
                "status_updated_at,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, name, store_name, dir_name, size, note or "",
                 folder_id, status, now, now))
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

    def list_contracts(self, user_id, folder_id=None, status=None, q=None,
                       limit=None, offset=0):
        sql = ("SELECT c.id,c.name,c.store_name,c.dir,c.size,c.note,c.folder_id,"
               "c.status,c.current_job_id,c.last_error,c.status_updated_at,"
               "c.created_at,f.name AS folder_name FROM contract_files c "
               "LEFT JOIN folders f ON f.id=c.folder_id WHERE c.user_id=%s ")
        args = [user_id]
        if folder_id is not None:
            sql += "AND c.folder_id=%s "
            args.append(folder_id)
        if status:
            sql += "AND c.status=%s "
            args.append(status)
        if q:
            like = "%" + q.replace("%", "") + "%"
            sql += "AND (c.name LIKE %s OR IFNULL(c.store_name,'') LIKE %s) "
            args.extend([like, like])
        sql += "ORDER BY c.created_at DESC, c.id DESC"
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            args.extend([int(limit), int(offset or 0)])
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

    def set_contract_status(self, user_id, contract_id, status, *,
                            job_id=None, error=None,
                            touch_job=False, touch_error=False) -> bool:
        from store.contract_status import STATUSES, can_transition, IllegalTransition
        if status not in STATUSES:
            raise IllegalTransition(f"未知状态：{status}")
        row = self.get_contract(user_id, contract_id)
        if not row:
            return False
        src = row.get("status") or "ready"
        if not can_transition(src, status):
            raise IllegalTransition(f"不允许从 {src} 变为 {status}")
        sets = ["status=%s", "status_updated_at=%s"]
        args = [status, _now()]
        if touch_job:
            sets.append("current_job_id=%s")
            args.append(job_id or None)
        if touch_error:
            text = error or ""
            if len(text) > 500:
                text = text[:499] + "…"
            sets.append("last_error=%s")
            args.append(text)
        args += [user_id, contract_id]
        conn, n = self._q(
            f"UPDATE contract_files SET {', '.join(sets)} "
            "WHERE user_id=%s AND id=%s", args)
        conn.close()
        return n > 0

    def _txn(self):
        return self._pymysql.connect(
            **self._cfg, database=self._db, autocommit=False,
            cursorclass=self._pymysql.cursors.DictCursor)

    def delete_contract(self, user_id, contract_id):
        conn = self._txn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM review_findings WHERE user_id=%s AND contract_id=%s",
                    (user_id, contract_id))
                n = cur.execute(
                    "DELETE FROM contract_files WHERE user_id=%s AND id=%s",
                    (user_id, contract_id))
            conn.commit()
            return n > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def replace_contract_findings(self, user_id, contract_id, job_id, rows) -> int:
        from store.findings import merge_prior_decisions, normalize_row
        if not self.get_contract(user_id, contract_id):
            return 0
        prior = self.list_findings(user_id, contract_id)
        clean = merge_prior_decisions([normalize_row(r) for r in (rows or [])], prior)
        now = _now()
        jid = (job_id or "")[:64]
        conn = self._txn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM review_findings WHERE user_id=%s AND contract_id=%s",
                    (user_id, contract_id))
                for r in clean:
                    cur.execute(
                        "INSERT INTO review_findings("
                        "contract_id,user_id,dimension,severity,title,detail,quote,"
                        "location,status,note,job_id,created_at,updated_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (contract_id, user_id, r["dimension"], r["severity"], r["title"],
                         r["detail"], r["quote"], r["location"],
                         r.get("status") or "open", r.get("note") or "",
                         jid, now, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(clean)

    def list_findings(self, user_id, contract_id, status=None):
        sql = "SELECT * FROM review_findings WHERE user_id=%s AND contract_id=%s "
        args = [user_id, contract_id]
        if status:
            sql += "AND status=%s "
            args.append(status)
        sql += "ORDER BY id ASC"
        return self._fetch(sql, args) or []

    def get_finding(self, user_id, finding_id):
        return self._fetch(
            "SELECT * FROM review_findings WHERE user_id=%s AND id=%s",
            (user_id, finding_id), one=True)

    def update_finding(self, user_id, finding_id, *, status=None, note=None):
        from store.findings import STATUSES
        row = self.get_finding(user_id, finding_id)
        if not row:
            return None
        if status is None and note is None:
            return row
        if status is not None and status not in STATUSES:
            raise ValueError("未知处理状态")
        new_status = status if status is not None else row.get("status") or "open"
        new_note = note if note is not None else (row.get("note") or "")
        if len(new_note) > 500:
            new_note = new_note[:499] + "…"
        conn, _n = self._q(
            "UPDATE review_findings SET status=%s, note=%s, updated_at=%s "
            "WHERE user_id=%s AND id=%s",
            (new_status, new_note, _now(), user_id, finding_id))
        conn.close()
        return self.get_finding(user_id, finding_id)

    def findings_summary(self, user_id, contract_id):
        from store.findings import SEVERITIES, STATUSES
        if not self.get_contract(user_id, contract_id):
            return None
        by_sev = {k: 0 for k in SEVERITIES}
        by_st = {k: 0 for k in STATUSES}
        grouped = self._fetch(
            "SELECT severity, status, COUNT(*) AS n FROM review_findings "
            "WHERE user_id=%s AND contract_id=%s GROUP BY severity, status",
            (user_id, contract_id)) or []
        total = 0
        for r in grouped:
            n = int(r.get("n") or 0)
            total += n
            if r.get("severity") in by_sev:
                by_sev[r["severity"]] += n
            if r.get("status") in by_st:
                by_st[r["status"]] += n
        return {
            "contract_id": contract_id,
            "total": total,
            "open": by_st.get("open", 0),
            "by_severity": by_sev,
            "by_status": by_st,
        }

    def open_finding_counts(self, user_id) -> dict:
        rows = self._fetch(
            "SELECT contract_id, COUNT(*) AS n FROM review_findings "
            "WHERE user_id=%s AND status='open' GROUP BY contract_id",
            (user_id,)) or []
        return {int(r["contract_id"]): int(r["n"]) for r in rows}

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
        conn = self._txn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM contract_files WHERE user_id=%s AND name=%s",
                    (user_id, name))
                ids = [r["id"] for r in cur.fetchall()]
                for cid in ids:
                    cur.execute(
                        "DELETE FROM review_findings WHERE user_id=%s AND contract_id=%s",
                        (user_id, cid))
                n = cur.execute(
                    "DELETE FROM contract_files WHERE user_id=%s AND name=%s",
                    (user_id, name))
            conn.commit()
            return n > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
