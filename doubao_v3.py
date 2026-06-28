# -*- coding: utf-8 -*-
"""Doubao video generation core with a cookie-pool scheduler.

This module intentionally works from user-provided cookie JSON files. It does
not read Chrome profiles, browser cookie stores, localStorage, or sessionStorage.
"""

from __future__ import annotations

import json
import base64
import mimetypes
import os
import random
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


@dataclass
class Config:
    runtime_dir: str = os.environ.get("DOUBAO_RUNTIME_DIR") or os.path.dirname(os.path.abspath(__file__))
    cookies_dir: str = os.environ.get("COOKIES_DIR", "")
    data_dir: str = os.environ.get("DATA_DIR", "")
    output_dir: str = os.environ.get("OUTPUT_DIR", "")
    base_url: str = os.environ.get("DOUBAO_BASE_URL", "https://www.doubao.com").rstrip("/")
    chat_referer: str = os.environ.get("DOUBAO_REFERER", "https://www.doubao.com/chat/")
    user_agent: str = os.environ.get(
        "DOUBAO_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    api_timeout: int = int(os.environ.get("API_TIMEOUT", "90"))
    submit_timeout: int = int(os.environ.get("SUBMIT_TIMEOUT", "300"))
    poll_interval: int = int(os.environ.get("POLL_INTERVAL", "10"))
    task_timeout: int = int(os.environ.get("TASK_TIMEOUT", "3600"))
    min_api_interval: float = float(os.environ.get("MIN_API_INTERVAL", "2.0"))


config = Config()
config.cookies_dir = config.cookies_dir or os.path.join(config.runtime_dir, "cookies")
config.data_dir = config.data_dir or os.path.join(config.runtime_dir, "data")
config.output_dir = config.output_dir or os.path.join(config.runtime_dir, "downloads")

DB_PATH = os.path.join(config.data_dir, "doubao_tasks.db")
RUNTIME_CONFIG_PATH = os.path.join(config.data_dir, "doubao_runtime_config.json")

COOKIE_MIN_SUBMIT_CREDITS = max(1, int(os.environ.get("COOKIE_MIN_SUBMIT_CREDITS", "1")))
COOKIE_SUBMIT_MIN_INTERVAL_SECONDS = max(0.0, float(os.environ.get("COOKIE_SUBMIT_MIN_INTERVAL_SECONDS", "20")))
COOKIE_RATE_LIMIT_COOLDOWN_SECONDS = max(30, int(os.environ.get("COOKIE_RATE_LIMIT_COOLDOWN_SECONDS", "75")))
DOUBAO_VIDEO_TASK_COST = max(1, int(os.environ.get("DOUBAO_VIDEO_TASK_COST", "2")))

DOUBAO_VIDEO_SKILL_TYPE = 17
DOUBAO_VIDEO_CONTENT_TYPE = 2020
DOUBAO_MESSAGE_FROM_INPUT_BOX = 0
DOUBAO_UPLOAD_TENANT_ID = "5"
DOUBAO_UPLOAD_SCENE_ID = "5"
DOUBAO_IMAGE_RESOURCE_TYPE = 2
DOUBAO_IMAGE_ATTACHMENT_TYPE = "image"
DOUBAO_ATTACHMENT_TYPE_IMAGE_ENUM = 2
DOUBAO_ATTACHMENT_FILE_TYPE_OCR_IMAGE = 2
DOUBAO_PARSE_STATE_SUCCESS = 1
DOUBAO_REVIEW_STATE_ACCESS = 1
DISABLED_COOKIE_STATUSES = {"disabled", "inactive", "off", "paused"}
DOUBAO_DEFAULT_COMMON_PARAMS = {
    "aid": "497858",
    "real_aid": "497858",
    "version_code": "20800",
    "language": "zh",
    "device_platform": "web",
    "pkg_type": "release_version",
    "pc_version": "3.23.8",
    "web_platform": "browser",
    "samantha_web": "1",
    "use-olympus-account": "1",
    "region": "CN",
    "sys_region": "CN",
}
DOUBAO_RUNTIME_REQUIRED_COMMON_KEYS = ("aid", "device_id", "web_id", "web_tab_id")
DOUBAO_RUNTIME_ID_KEYS = ("device_id", "web_id", "tea_uuid")


def log(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:
        pass


class ErrorCode(Enum):
    SUCCESS = (0, "成功", 200)
    PARAMS_INVALID = (-2000, "请求参数非法", 400)
    REQUEST_FAILED = (-2001, "请求失败", 500)
    TOKEN_EXPIRED = (-2002, "Cookie 已失效", 401)
    COOKIE_INVALID = (-2003, "Cookie 文件无效", 400)
    CONTENT_FILTERED = (-2006, "内容审核未通过", 400)
    VIDEO_FAILED = (-2008, "视频生成失败", 500)
    INSUFFICIENT_CREDITS = (-2009, "生成次数不足", 402)
    TIMEOUT = (-2011, "操作超时", 504)
    RATE_LIMITED = (-2012, "操作过于频繁", 429)


class APIException(Exception):
    def __init__(self, code: ErrorCode, detail: str = ""):
        self.code = code
        self.detail = detail
        self.message = f"{code.value[1]}: {detail}" if detail else code.value[1]
        super().__init__(self.message)


class PerAccountRateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last_request_at: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait_if_needed(self, cookie_name: str) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            last = self._last_request_at.get(cookie_name, 0.0)
            remaining = self.min_interval - (time.time() - last)
        if remaining > 0:
            time.sleep(remaining)

    def record_request(self, cookie_name: str) -> None:
        with self._lock:
            self._last_request_at[cookie_name] = time.time()


rate_limiter = PerAccountRateLimiter(config.min_api_interval)


def ensure_runtime_dirs() -> None:
    os.makedirs(config.cookies_dir, exist_ok=True)
    os.makedirs(config.data_dir, exist_ok=True)
    os.makedirs(config.output_dir, exist_ok=True)


def get_db_connection() -> sqlite3.Connection:
    ensure_runtime_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_column(cursor: sqlite3.Cursor, table: str, name: str, definition: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cursor.fetchall()}
    if name not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_core_database() -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cookies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            file_path TEXT NOT NULL,
            credits INTEGER DEFAULT 0,
            last_used TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT
        )
        """
    )
    _ensure_column(cursor, "cookies", "remain_count", "INTEGER")
    _ensure_column(cursor, "cookies", "has_generating_task", "INTEGER DEFAULT 0")
    _ensure_column(cursor, "cookies", "is_beta_user", "INTEGER DEFAULT 0")
    _ensure_column(cursor, "cookies", "last_error", "TEXT")
    _ensure_column(cursor, "cookies", "updated_at", "TEXT")
    conn.commit()
    conn.close()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def cookie_record_name(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename))[0]


def normalize_cookie_filename(name: str, fallback_prefix: str = "cookie") -> str:
    raw_name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    if raw_name.lower().endswith(".json"):
        raw_name = raw_name[:-5]
    safe_name = "".join(ch if ch not in '<>:"/\\|?*\r\n\t' and ord(ch) >= 32 else "_" for ch in raw_name)
    safe_name = safe_name.strip(" ._")
    if not safe_name:
        safe_name = f"{fallback_prefix}_{int(time.time())}"
    return safe_name + ".json"


def _cookie_identity(cookie_file: str) -> str:
    return os.path.abspath(_resolve_cookie_file_path(cookie_file))


def _resolve_cookie_file_path(cookie_file: str) -> str:
    return cookie_file if os.path.dirname(cookie_file) else os.path.join(config.cookies_dir, cookie_file)


def load_cookies(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Cookie 文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
        raw = raw["cookies"]
    elif isinstance(raw, dict) and isinstance(raw.get("cookie"), str):
        raw = parse_cookie_header(raw["cookie"])
    elif isinstance(raw, dict):
        raw = [{"name": key, "value": value, "domain": ".doubao.com", "path": "/"} for key, value in raw.items()]

    if not isinstance(raw, list):
        raise ValueError("Cookie 文件必须是数组，或包含 cookies 数组的对象")

    cleaned: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            continue
        cookie = {
            "name": name,
            "value": str(value),
            "domain": item.get("domain") or ".doubao.com",
            "path": item.get("path") or "/",
        }
        for key in ("expires", "expirationDate", "httpOnly", "secure", "sameSite"):
            if key in item and item[key] is not None:
                cookie[key] = item[key]
        cleaned.append(cookie)

    if not cleaned:
        raise ValueError("Cookie 文件为空或格式错误")
    return cleaned


def parse_cookie_header(cookie_header: str) -> List[Dict[str, str]]:
    cookies = []
    for part in str(cookie_header or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            cookies.append({"name": name, "value": value.strip(), "domain": ".doubao.com", "path": "/"})
    return cookies


def cookies_to_header(cookies: Iterable[Dict[str, Any]], domain_hint: str = "doubao.com") -> str:
    pairs = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if domain_hint and domain and domain_hint not in domain:
            continue
        name = str(cookie.get("name") or "").strip()
        value = cookie.get("value")
        if name and value is not None:
            pairs.append(f"{name}={value}")
    if not pairs:
        raise APIException(ErrorCode.COOKIE_INVALID, "Cookie 中没有 doubao.com 相关字段")
    return "; ".join(pairs)


def get_cookie_record_status(name_or_filename: str, default: str = "active") -> str:
    init_core_database()
    name = cookie_record_name(name_or_filename)
    filename = name + ".json"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT status FROM cookies
            WHERE name IN (?, ?)
            ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (name, filename, name),
        )
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return default


def is_cookie_enabled(cookie_file: str) -> bool:
    return get_cookie_record_status(cookie_file).strip().lower() not in DISABLED_COOKIE_STATUSES


def get_token_files(active_only: bool = True) -> List[str]:
    ensure_runtime_dirs()
    files = sorted([item for item in os.listdir(config.cookies_dir) if item.lower().endswith(".json")])
    if not active_only:
        return files
    return [item for item in files if is_cookie_enabled(item)]


def read_cookie_credits(cookie_file: str) -> Optional[int]:
    init_core_database()
    name = cookie_record_name(cookie_file)
    filename = name + ".json"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(remain_count, credits) FROM cookies
            WHERE name IN (?, ?)
            ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (name, filename, name),
        )
        row = cursor.fetchone()
        conn.close()
        if not row or row[0] is None:
            return None
        return int(row[0])
    except Exception as exc:
        log(f"[WARN] 读取 Cookie 额度缓存失败: {name} ({exc})")
        return None


def update_cookie_record(
    cookie_file: str,
    credits: Optional[int] = None,
    status: Optional[str] = None,
    last_error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    init_core_database()
    fpath = _resolve_cookie_file_path(cookie_file)
    name = cookie_record_name(cookie_file)
    current_status = get_cookie_record_status(name)
    next_status = status or current_status or "active"
    if current_status.strip().lower() in DISABLED_COOKIE_STATUSES and status == "active":
        next_status = current_status

    extra = extra or {}
    remain_count = extra.get("remain_count", credits)
    has_generating_task = 1 if extra.get("has_generating_task") else 0
    is_beta_user = 1 if extra.get("is_beta_user") else 0
    now = now_iso()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO cookies
            (name, file_path, credits, remain_count, has_generating_task,
             is_beta_user, last_error, last_used, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            file_path=excluded.file_path,
            credits=excluded.credits,
            remain_count=excluded.remain_count,
            has_generating_task=excluded.has_generating_task,
            is_beta_user=excluded.is_beta_user,
            last_error=excluded.last_error,
            last_used=excluded.last_used,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (
            name,
            os.path.abspath(fpath),
            int(credits) if credits is not None else None,
            int(remain_count) if remain_count is not None else None,
            has_generating_task,
            is_beta_user,
            last_error,
            now,
            next_status,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()


def update_cookie_status(filename: str, enabled: bool) -> Dict[str, Any]:
    safe_filename = normalize_cookie_filename(filename)
    fpath = os.path.join(config.cookies_dir, safe_filename)
    if not os.path.exists(fpath):
        raise FileNotFoundError("Cookie 文件不存在")
    status = "active" if enabled else "disabled"
    update_cookie_record(safe_filename, credits=read_cookie_credits(safe_filename), status=status)
    return {
        "name": cookie_record_name(safe_filename),
        "filename": safe_filename,
        "status": status,
        "enabled": enabled,
    }


_cookie_rotation_lock = threading.Lock()
_cookie_rotation_next = 0
_cookie_reserved_credits: Dict[str, int] = {}
_cookie_active_counts: Dict[str, int] = {}
_cookie_submit_next_at: Dict[str, float] = {}
_cookie_rate_limit_until: Dict[str, float] = {}


def estimate_required_credits(model: str = "doubao-seedance-2.0", duration: int = 5) -> int:
    return DOUBAO_VIDEO_TASK_COST


def refresh_cookie_credits(cookie_file: str, credits: Optional[int], extra: Optional[Dict[str, Any]] = None) -> None:
    if credits is None:
        return
    with _cookie_rotation_lock:
        update_cookie_record(cookie_file, credits=max(0, int(credits)), status="active", extra=extra)


def consume_cookie_credits(cookie_file: str, used_credits: int) -> None:
    if used_credits <= 0:
        return
    with _cookie_rotation_lock:
        key = _cookie_identity(cookie_file)
        reserved = max(0, _cookie_reserved_credits.get(key, 0) - used_credits)
        if reserved:
            _cookie_reserved_credits[key] = reserved
        else:
            _cookie_reserved_credits.pop(key, None)

        current = read_cookie_credits(cookie_file)
        if current is None:
            return
        update_cookie_record(cookie_file, credits=max(0, current - used_credits), status="active")


def release_cookie_slot(cookie_file: str, reserved_credits: int) -> None:
    key = _cookie_identity(cookie_file)
    with _cookie_rotation_lock:
        active = max(0, _cookie_active_counts.get(key, 0) - 1)
        reserved = max(0, _cookie_reserved_credits.get(key, 0) - max(0, reserved_credits))
        if active:
            _cookie_active_counts[key] = active
        else:
            _cookie_active_counts.pop(key, None)
        if reserved:
            _cookie_reserved_credits[key] = reserved
        else:
            _cookie_reserved_credits.pop(key, None)


def get_cookie_attempt_order(cookie_files: List[str]) -> List[str]:
    global _cookie_rotation_next
    files = list(cookie_files)
    if len(files) <= 1:
        return files
    with _cookie_rotation_lock:
        start = _cookie_rotation_next % len(files)
        _cookie_rotation_next = (start + 1) % len(files)
    return files[start:] + files[:start]


def mark_cookie_used(cookie_files: List[str], used_cookie_file: str) -> None:
    global _cookie_rotation_next
    if len(cookie_files) <= 1:
        return
    used_key = _cookie_identity(used_cookie_file)
    keys = [_cookie_identity(item) for item in cookie_files]
    try:
        index = keys.index(used_key)
    except ValueError:
        return
    with _cookie_rotation_lock:
        _cookie_rotation_next = (index + 1) % len(cookie_files)


def mark_cookie_rate_limited(cookie_file: str, cooldown_seconds: Optional[int] = None) -> None:
    cooldown = int(cooldown_seconds or COOKIE_RATE_LIMIT_COOLDOWN_SECONDS)
    until = time.time() + max(1, cooldown)
    key = _cookie_identity(cookie_file)
    with _cookie_rotation_lock:
        _cookie_rate_limit_until[key] = max(_cookie_rate_limit_until.get(key, 0.0), until)
        _cookie_submit_next_at[key] = max(_cookie_submit_next_at.get(key, 0.0), until)


def wait_for_cookie_submit_slot(cookie_file: str) -> None:
    if COOKIE_SUBMIT_MIN_INTERVAL_SECONDS <= 0:
        return
    key = _cookie_identity(cookie_file)
    while True:
        with _cookie_rotation_lock:
            now = time.time()
            next_at = max(_cookie_submit_next_at.get(key, 0.0), _cookie_rate_limit_until.get(key, 0.0))
            remaining = next_at - now
            if remaining <= 0:
                _cookie_submit_next_at[key] = now + COOKIE_SUBMIT_MIN_INTERVAL_SECONDS
                return
        time.sleep(max(1.0, min(remaining, 10.0)))


def acquire_cookie_slot(cookie_files: List[str], required_credits: int, allow_unknown_credits: bool = True) -> str:
    minimum = max(required_credits, COOKIE_MIN_SUBMIT_CREDITS)
    while True:
        should_wait = False
        min_wait = None
        with _cookie_rotation_lock:
            now = time.time()
            prefer_idle_passes = [True, False] if len(cookie_files) > 1 else [False]
            for idle_only in prefer_idle_passes:
                for cookie_file in cookie_files:
                    if not is_cookie_enabled(cookie_file):
                        continue
                    key = _cookie_identity(cookie_file)
                    active = _cookie_active_counts.get(key, 0)
                    if idle_only and active > 0:
                        continue
                    cooldown = max(0.0, _cookie_rate_limit_until.get(key, 0.0) - now)
                    known = read_cookie_credits(cookie_file)
                    reserved = _cookie_reserved_credits.get(key, 0)
                    if cooldown > 0:
                        if known is None or int(known) >= minimum:
                            should_wait = True
                            min_wait = cooldown if min_wait is None else min(min_wait, cooldown)
                        continue
                    if known is None:
                        if allow_unknown_credits:
                            _cookie_active_counts[key] = active + 1
                            _cookie_reserved_credits[key] = reserved + required_credits
                            return cookie_file
                        continue
                    if int(known) - reserved >= minimum:
                        _cookie_active_counts[key] = active + 1
                        _cookie_reserved_credits[key] = reserved + required_credits
                        return cookie_file
        if not should_wait:
            raise APIException(ErrorCode.INSUFFICIENT_CREDITS, f"没有可用次数 >= {minimum} 的 Cookie")
        time.sleep(max(1.0, min(min_wait or 2.0, 10.0)))


def _load_json_env(name: str) -> Dict[str, Any]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: items[-1] if items else "" for key, items in value.items()}
    return value if isinstance(value, dict) else {}


_runtime_config_lock = threading.RLock()


def _has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value <= 0:
        return "0"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result


def _generate_web_identity() -> str:
    return str(random.randint(10**18, 10**19 - 1))


def _generate_web_tab_id() -> str:
    return uuid.uuid4().hex


def _generate_verify_fp() -> str:
    return f"verify_{_base36(int(time.time() * 1000))}_{str(uuid.uuid4()).replace('-', '_')}"


def _empty_runtime_config() -> Dict[str, Any]:
    return {"common_params": {}, "fp": ""}


def _read_runtime_config_file() -> Dict[str, Any]:
    ensure_runtime_dirs()
    if not os.path.exists(RUNTIME_CONFIG_PATH):
        return _empty_runtime_config()
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "common_params": data.get("common_params") if isinstance(data.get("common_params"), dict) else {},
            "fp": str(data.get("fp") or ""),
            "updated_at": data.get("updated_at"),
        }
    except Exception:
        return _empty_runtime_config()


def _write_runtime_config_file(config_data: Dict[str, Any]) -> None:
    ensure_runtime_dirs()
    config_data["updated_at"] = now_iso()
    with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)


def _apply_runtime_defaults(config_data: Dict[str, Any]) -> bool:
    changed = False
    common = config_data.get("common_params")
    if not isinstance(common, dict):
        common = {}
        config_data["common_params"] = common
        changed = True

    for key, value in DOUBAO_DEFAULT_COMMON_PARAMS.items():
        if not _has_value(common.get(key)):
            common[key] = value
            changed = True

    browser_id = next((str(common.get(key)).strip() for key in DOUBAO_RUNTIME_ID_KEYS if _has_value(common.get(key))), "")
    if not browser_id:
        browser_id = _generate_web_identity()
        changed = True
    for key in DOUBAO_RUNTIME_ID_KEYS:
        if not _has_value(common.get(key)):
            common[key] = browser_id
            changed = True

    if not _has_value(common.get("web_tab_id")):
        common["web_tab_id"] = _generate_web_tab_id()
        changed = True
    if not _has_value(config_data.get("fp")):
        config_data["fp"] = _generate_verify_fp()
        changed = True
    return changed


def ensure_runtime_config() -> Dict[str, Any]:
    with _runtime_config_lock:
        current = _read_runtime_config_file()
        if _apply_runtime_defaults(current):
            _write_runtime_config_file(current)
        return current


def load_runtime_config() -> Dict[str, Any]:
    return ensure_runtime_config()


def save_runtime_config(common_params: Optional[Dict[str, Any]] = None, fp: Optional[str] = None) -> Dict[str, Any]:
    with _runtime_config_lock:
        current = _read_runtime_config_file()
        if common_params is not None:
            normalized = {str(key): str(value).strip() for key, value in common_params.items() if _has_value(value)}
            if fp is None and normalized.get("fp"):
                fp = normalized.pop("fp")
            else:
                normalized.pop("fp", None)
            current["common_params"] = normalized
        if fp is not None:
            current["fp"] = str(fp).strip()
        _apply_runtime_defaults(current)
        _write_runtime_config_file(current)
        return current


def _cookie_values(cookie_file: Optional[str]) -> Dict[str, str]:
    if not cookie_file:
        return {}
    try:
        cookies = load_cookies(_resolve_cookie_file_path(cookie_file))
    except Exception:
        return {}
    values: Dict[str, str] = {}
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = cookie.get("value")
        if name and value is not None:
            values[name] = str(value)
    return values


def _find_json_value(value: Any, names: Tuple[str, ...]) -> Optional[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in names and _has_value(child):
                return str(child).strip()
            found = _find_json_value(child, names)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_json_value(child, names)
            if found:
                return found
    return None


def _extract_runtime_id_from_cookie_value(raw_value: str) -> Optional[str]:
    value = urllib.parse.unquote(str(raw_value or "").strip())
    if re.fullmatch(r"\d{10,24}", value):
        return value
    try:
        parsed = json.loads(value)
    except Exception:
        parsed = None
    if parsed is not None:
        found = _find_json_value(parsed, ("web_id", "user_unique_id", "tea_uuid", "device_id"))
        if found and re.fullmatch(r"\d{10,24}", found):
            return found
    match = re.search(r'"(?:web_id|user_unique_id|tea_uuid|device_id)"\s*:\s*"?(?P<id>\d{10,24})"?', value)
    return match.group("id") if match else None


def _cookie_runtime_params(cookie_file: Optional[str]) -> Dict[str, str]:
    values = _cookie_values(cookie_file)
    params: Dict[str, str] = {}
    country = str(values.get("flow_user_country") or "").strip().upper()
    if country:
        params["region"] = country
        params["sys_region"] = country

    for name in ("__tea__ug__uid", "_tea_utm_cache_497858", "_tea_utm_cache_586864"):
        runtime_id = _extract_runtime_id_from_cookie_value(values.get(name, ""))
        if runtime_id:
            params["web_id"] = runtime_id
            params["tea_uuid"] = runtime_id
            params["device_id"] = runtime_id
            break
    return params


def _cookie_runtime_fp(cookie_file: Optional[str]) -> str:
    values = _cookie_values(cookie_file)
    fp = str(values.get("s_v_web_id") or "").strip()
    if fp:
        return fp
    return ""


def common_query_params(cookie_file: Optional[str] = None) -> Dict[str, str]:
    runtime = ensure_runtime_config()
    params: Dict[str, str] = {
        str(k): str(v) for k, v in runtime.get("common_params", {}).items() if v is not None
    }
    params.update(_cookie_runtime_params(cookie_file))
    params.update({str(k): str(v) for k, v in _load_json_env("DOUBAO_COMMON_PARAMS").items() if v is not None})
    params.update({str(k): str(v) for k, v in _load_json_env("DOUBAO_QUERY_PARAMS").items() if v is not None})
    return params


def runtime_fp(cookie_file: Optional[str] = None) -> str:
    return (
        os.environ.get("DOUBAO_FP", "").strip()
        or _cookie_runtime_fp(cookie_file)
        or str(ensure_runtime_config().get("fp") or "").strip()
    )


def runtime_config_diagnostics(cookie_file: Optional[str] = None) -> Dict[str, Any]:
    params = common_query_params(cookie_file)
    fp = runtime_fp(cookie_file)
    expected = list(DOUBAO_RUNTIME_REQUIRED_COMMON_KEYS)
    missing = [key for key in expected if not params.get(key)]
    if not fp:
        missing.append("fp")
    return {
        "ready": not missing,
        "missing": missing,
        "params_count": len(params),
        "has_fp": bool(fp),
        "common_params": params,
        "fp": fp,
        "config_path": RUNTIME_CONFIG_PATH,
    }


def build_url(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    include_common: bool = True,
    include_fp: bool = False,
    cookie_file: Optional[str] = None,
) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        url = config.base_url + (path if path.startswith("/") else "/" + path)

    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    if include_common:
        query.update(common_query_params(cookie_file))
    if params:
        query.update({str(k): str(v) for k, v in params.items() if v is not None})
    fp = runtime_fp(cookie_file)
    if include_fp and fp and "fp" not in query:
        query["fp"] = fp
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment))


def _cookie_headers(cookie_file: str, content_type: str = "application/json", extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    cookies = load_cookies(_resolve_cookie_file_path(cookie_file))
    headers = {
        "User-Agent": config.user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Content-Type": content_type,
        "Origin": config.base_url,
        "Referer": config.chat_referer,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Priority": "u=1, i",
        "Cookie": cookies_to_header(cookies),
    }
    if extra:
        headers.update(extra)
    return headers


def _read_error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")
    except Exception:
        return str(error)


def classify_http_error(status: int, body: str) -> APIException:
    lowered = (body or "").lower()
    if status == 400 and not (body or "").strip():
        diag = runtime_config_diagnostics()
        if not diag["ready"]:
            return APIException(
                ErrorCode.REQUEST_FAILED,
                "HTTP 400: 豆包拒绝了生成请求，当前缺少运行时参数 "
                + ", ".join(diag["missing"])
                + "。请在前端“运行时参数”里填写豆包请求 URL 中的通用参数和 fp 后重试。",
            )
        return APIException(
            ErrorCode.REQUEST_FAILED,
            "HTTP 400: 豆包拒绝了生成请求但没有返回错误内容，可能是 fp 已过期或请求协议仍需补齐。",
        )
    if status in (401, 403) or "login" in lowered or "session" in lowered:
        return APIException(ErrorCode.TOKEN_EXPIRED, body[:500])
    if status == 429 or "rate" in lowered or "frequency" in lowered or "频繁" in body:
        return APIException(ErrorCode.RATE_LIMITED, body[:500])
    if "credit" in lowered or "quota" in lowered or "remain" in lowered or "次数不足" in body or "额度不足" in body:
        return APIException(ErrorCode.INSUFFICIENT_CREDITS, body[:500])
    if "block" in lowered or "risk" in lowered or "审核" in body:
        return APIException(ErrorCode.CONTENT_FILTERED, body[:500])
    return APIException(ErrorCode.REQUEST_FAILED, f"HTTP {status}: {body[:500]}")


def post_json(
    cookie_file: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
    include_common: bool = True,
    include_fp: bool = False,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Any:
    cookie_name = cookie_record_name(cookie_file)
    rate_limiter.wait_if_needed(cookie_name)
    url = build_url(path, params=params, include_common=include_common, include_fp=include_fp, cookie_file=cookie_file)
    data = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=_cookie_headers(cookie_file, extra=extra_headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or config.api_timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise classify_http_error(exc.code, _read_error_body(exc)) from exc
    except urllib.error.URLError as exc:
        raise APIException(ErrorCode.REQUEST_FAILED, str(exc)) from exc
    finally:
        rate_limiter.record_request(cookie_name)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _walk_json(value: Any, _depth: int = 0) -> Iterable[Any]:
    yield value
    if _depth > 20:
        return
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child, _depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child, _depth + 1)
    elif isinstance(value, str):
        text = value.strip()
        if len(text) >= 2 and text[0] in "{[":
            try:
                yield from _walk_json(json.loads(text), _depth + 1)
            except json.JSONDecodeError:
                pass


def extract_remain_count(payload: Any) -> Optional[int]:
    for item in _walk_json(payload):
        if not isinstance(item, dict):
            continue
        for key in ("remain_count", "remainCount", "remaining_count", "available_count"):
            if key in item and item[key] is not None:
                try:
                    return int(item[key])
                except (TypeError, ValueError):
                    pass
    return None


def parse_video_gen_info(payload: Any) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "remain_count": extract_remain_count(payload),
        "has_generating_task": False,
        "is_beta_user": False,
        "raw": payload,
    }
    for item in _walk_json(payload):
        if isinstance(item, dict):
            if "has_generating_task" in item:
                info["has_generating_task"] = bool(item.get("has_generating_task"))
            if "is_beta_user" in item:
                info["is_beta_user"] = bool(item.get("is_beta_user"))
    return info


def get_video_gen_info(cookie_file: str) -> Dict[str, Any]:
    payload = post_json(cookie_file, "/samantha/video/query_video_gen_info", {}, include_fp=False)
    info = parse_video_gen_info(payload)
    if info.get("remain_count") is not None:
        update_cookie_record(cookie_file, credits=info["remain_count"], status="active", extra=info)
    return info


def test_cookie_file(cookie_file: str) -> Dict[str, Any]:
    fpath = _resolve_cookie_file_path(cookie_file)
    load_cookies(fpath)
    try:
        info = get_video_gen_info(fpath)
        update_cookie_record(fpath, credits=info.get("remain_count"), status="active", extra=info, last_error=None)
        return {
            "status": "success",
            "cookie_name": cookie_record_name(cookie_file),
            "credits": info.get("remain_count"),
            "remain_count": info.get("remain_count"),
            "has_generating_task": info.get("has_generating_task"),
            "is_beta_user": info.get("is_beta_user"),
            "raw": info.get("raw"),
        }
    except APIException as exc:
        status = "disabled" if exc.code == ErrorCode.TOKEN_EXPIRED else get_cookie_record_status(cookie_file)
        update_cookie_record(fpath, credits=read_cookie_credits(cookie_file), status=status, last_error=exc.message)
        raise


def refresh_cookie_pool_credits(cookie_files: List[str]) -> Dict[str, Optional[int]]:
    results: Dict[str, Optional[int]] = {}
    for cookie_file in cookie_files:
        fpath = _resolve_cookie_file_path(cookie_file)
        try:
            info = get_video_gen_info(fpath)
            results[_cookie_identity(fpath)] = info.get("remain_count")
        except Exception as exc:
            log(f"[WARN] Cookie 额度预查失败: {os.path.basename(fpath)} ({exc})")
            results[_cookie_identity(fpath)] = None
    return results


def remove_none(value: Dict[str, Any]) -> Dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def remove_none_deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: remove_none_deep(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [remove_none_deep(item) for item in value]
    return value


def normalize_ratio(ratio: str) -> str:
    ratio = str(ratio or "16:9").strip()
    if ratio in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
        return ratio
    return "16:9"


def build_video_content(prompt: str, ratio: str = "16:9", camera_movement: Optional[str] = None) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise APIException(ErrorCode.PARAMS_INVALID, "提示词不能为空")
    if camera_movement is None and ("固定镜头" in text or "fixed camera" in text.lower()):
        camera_movement = "fixed"
    content = remove_none(
        {
            "text": text,
            "ratio": normalize_ratio(ratio),
            "camera_movement": camera_movement,
        }
    )
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def build_samantha_event(
    prompt: str,
    ratio: str = "16:9",
    attachments: Optional[List[Dict[str, Any]]] = None,
    conversation_id: Optional[str] = None,
    local_conversation_id: Optional[str] = None,
    message_from: int = DOUBAO_MESSAGE_FROM_INPUT_BOX,
    camera_movement: Optional[str] = None,
    launch_stage: Optional[int] = None,
    enable_commerce_credit: bool = False,
) -> Dict[str, Any]:
    local_message_id = "local_" + uuid.uuid4().hex
    local_conversation_id = local_conversation_id or ("local_" + uuid.uuid4().hex if not conversation_id else None)
    message = {
        "content": build_video_content(prompt, ratio, camera_movement=camera_movement),
        "content_type": DOUBAO_VIDEO_CONTENT_TYPE,
        "attachments": attachments or [],
    }
    completion_option = {
        "is_regen": False,
        "with_suggest": False,
        "need_create_conversation": not bool(conversation_id),
        "launch_stage": launch_stage,
        "is_replace": False,
        "is_delete": False,
        "is_ai_playground": False,
        "is_old_user": False,
        "action_bar_skill_id": DOUBAO_VIDEO_SKILL_TYPE,
        "message_from": message_from,
        "resend_for_regen": False,
        "enable_commerce_credit": bool(enable_commerce_credit),
    }
    return remove_none_deep(
        {
            "messages": [message],
            "completion_option": completion_option,
            "evaluate_option": {"web_ab_params": ""},
            "conversation_id": conversation_id,
            "local_conversation_id": local_conversation_id,
            "local_message_id": local_message_id,
            "message_id": None,
            "reply_id": None,
        }
    )


def parse_sse_text(text: str) -> List[Any]:
    events: List[Any] = []
    data_lines: List[str] = []
    for line in text.splitlines():
        if not line.strip():
            if data_lines:
                events.append(_parse_sse_data("\n".join(data_lines)))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        events.append(_parse_sse_data("\n".join(data_lines)))
    if not events and text.strip():
        events.append(_parse_sse_data(text.strip()))
    return events


def _parse_sse_data(data: str) -> Any:
    if data in {"[DONE]", "DONE"}:
        return {"event_type": "DONE"}
    try:
        outer = json.loads(data)
    except json.JSONDecodeError:
        return {"raw": data}
    if isinstance(outer, dict) and isinstance(outer.get("event_data"), str):
        try:
            outer["event_data_json"] = json.loads(outer["event_data"])
        except json.JSONDecodeError:
            pass
    return outer


def extract_vids(payload: Any) -> List[str]:
    vids: List[str] = []
    for item in _walk_json(payload):
        if not isinstance(item, dict):
            continue
        for key in ("vid", "video_id", "videoId", "item_id"):
            value = item.get(key)
            if isinstance(value, (str, int)) and str(value):
                text = str(value)
                if text not in vids:
                    vids.append(text)
    return vids


def _normalize_video_url(value: str) -> str:
    return str(value or "").replace("\\u0026", "&").replace("\\/", "/")


def _maybe_decode_base64_url(value: str) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text.startswith("http"):
        return None
    if len(text) < 16 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", text):
        return None
    padded = text + "=" * (-len(text) % 4)
    for candidate in (padded, padded.replace("-", "+").replace("_", "/")):
        try:
            decoded = base64.b64decode(candidate, validate=False).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if decoded.startswith("http"):
            return _normalize_video_url(decoded)
    return None


def _looks_like_video_url(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            ".mp4",
            "mime_type=video",
            "/video/",
            "/video/tos/",
            "/video/fplay/",
            "download=true",
            "logo_type=video_gen",
        )
    )


def extract_video_urls(payload: Any) -> List[str]:
    urls: List[str] = []
    pattern = re.compile(r"https?://[^\"'\s<>]+", re.IGNORECASE)
    for item in _walk_json(payload):
        if isinstance(item, str):
            for match in pattern.findall(item):
                match = _normalize_video_url(match)
                if _looks_like_video_url(match) and match not in urls:
                    urls.append(match)
            decoded = _maybe_decode_base64_url(item)
            if decoded and _looks_like_video_url(decoded) and decoded not in urls:
                urls.append(decoded)
        elif isinstance(item, dict):
            for key in ("main_url", "backup_url", "backup_url_1", "url", "play_url", "download_url", "video_url", "fallback_api"):
                value = item.get(key)
                if not isinstance(value, str):
                    continue
                decoded = _maybe_decode_base64_url(value)
                value = decoded or _normalize_video_url(value)
                if value.startswith("http") and _looks_like_video_url(value) and value not in urls:
                    urls.append(value)
    return urls


def _response_data(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


def _first_file_url(payload: Any) -> str:
    data = _response_data(payload)
    file_urls = data.get("file_urls")
    if isinstance(file_urls, list) and file_urls:
        item = file_urls[0]
        if isinstance(item, dict):
            return str(item.get("main_url") or item.get("back_url") or "")
    return ""


def get_doubao_file_url(cookie_file: str, uri: str, file_type: str = "image") -> str:
    if not uri:
        return ""
    try:
        payload = post_json(
            cookie_file,
            "/alice/message/get_file_url",
            {"uris": [uri], "type": file_type, "expire_second": 604800},
            include_common=True,
        )
        return _first_file_url(payload)
    except Exception as exc:
        log(f"[WARN] 获取豆包文件 URL 失败: {uri} ({exc})")
        return ""


def _uploader_bundle_path() -> str:
    explicit = os.environ.get("DOUBAO_TT_UPLOADER_BUNDLE", "").strip()
    if explicit:
        return explicit
    return os.path.join(tempfile.gettempdir(), "doubao-js-capture-current", "66418.d9bd974b.js")


def _node_helper_path() -> str:
    return os.path.join(config.runtime_dir, "server", "doubao_imagex_upload.cjs")


def _upload_auth_payload(raw: Dict[str, Any]) -> Dict[str, str]:
    access_key_id = raw.get("access_key") or raw.get("access_key_id") or raw.get("AccessKeyId") or raw.get("AccessKeyID") or ""
    secret_access_key = raw.get("secret_key") or raw.get("secret_access_key") or raw.get("SecretAccessKey") or ""
    session_token = raw.get("session_token") or raw.get("SessionToken") or ""
    expired_time = raw.get("expired_time") or raw.get("ExpiredTime") or ""
    current_time = raw.get("current_time") or raw.get("CurrentTime") or raw.get("currentTime") or ""
    return {
        "AccessKeyId": str(access_key_id),
        "AccessKeyID": str(access_key_id),
        "SecretAccessKey": str(secret_access_key),
        "SessionToken": str(session_token),
        "ExpiredTime": str(expired_time),
        "CurrentTime": str(current_time),
    }


def prepare_doubao_image_upload(cookie_file: str) -> Dict[str, Any]:
    payload = post_json(
        cookie_file,
        "/alice/resource/prepare_upload",
        {
            "tenant_id": DOUBAO_UPLOAD_TENANT_ID,
            "scene_id": DOUBAO_UPLOAD_SCENE_ID,
            "resource_type": DOUBAO_IMAGE_RESOURCE_TYPE,
        },
        include_common=True,
    )
    data = _response_data(payload)
    auth = data.get("upload_auth_token") if isinstance(data.get("upload_auth_token"), dict) else {}
    service_id = str(data.get("service_id") or "")
    if not service_id or not auth:
        raise APIException(ErrorCode.REQUEST_FAILED, f"豆包图片上传授权为空: {payload}")
    normalized_auth = _upload_auth_payload(auth)
    missing = [key for key in ("AccessKeyID", "SecretAccessKey", "SessionToken") if not normalized_auth.get(key)]
    if missing:
        raise APIException(ErrorCode.REQUEST_FAILED, f"豆包图片上传授权缺少字段: {', '.join(missing)}")
    return {
        "service_id": service_id,
        "auth": normalized_auth,
        "upload_host": str(data.get("upload_host") or ""),
        "upload_path_prefix": str(data.get("upload_path_prefix") or ""),
        "raw": data,
    }


def _image_uri_from_upload(upload_result: Dict[str, Any]) -> str:
    candidates = [
        upload_result.get("ImageUri"),
        upload_result.get("image_uri"),
        upload_result.get("uri"),
        upload_result.get("oid"),
        upload_result.get("key"),
    ]
    nested = upload_result.get("uploadResult")
    if isinstance(nested, dict):
        candidates = [
            nested.get("ImageUri"),
            nested.get("image_uri"),
            nested.get("uri"),
            nested.get("oid"),
            *candidates,
        ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return ""


def _upload_value(upload_result: Dict[str, Any], key: str, fallback: Any = None) -> Any:
    nested = upload_result.get("uploadResult")
    if isinstance(nested, dict) and nested.get(key) is not None:
        return nested.get(key)
    return upload_result.get(key, fallback)


def _run_doubao_image_upload_helper(cookie_file: str, attachment: Dict[str, Any], upload_info: Dict[str, Any]) -> Dict[str, Any]:
    local_path = str(attachment.get("local_path") or attachment.get("path") or "").strip()
    if not local_path or not os.path.exists(local_path):
        raise APIException(ErrorCode.PARAMS_INVALID, f"参考图不存在: {local_path}")
    helper_path = _node_helper_path()
    if not os.path.exists(helper_path):
        raise APIException(ErrorCode.REQUEST_FAILED, f"缺少豆包上传 helper: {helper_path}")

    content_type = str(attachment.get("mime") or mimetypes.guess_type(local_path)[0] or "application/octet-stream")
    request_body = {
        "filePath": local_path,
        "fileName": attachment.get("fileName") or attachment.get("name") or os.path.basename(local_path),
        "contentType": content_type,
        "serviceId": upload_info["service_id"],
        "stsToken": upload_info["auth"],
        "cookieHeader": cookies_to_header(load_cookies(cookie_file)),
        "appId": common_query_params(cookie_file).get("aid") or DOUBAO_DEFAULT_COMMON_PARAMS["aid"],
        "userId": _cookie_values(cookie_file).get("passport_user_id")
        or _cookie_values(cookie_file).get("uid")
        or _cookie_values(cookie_file).get("user_id")
        or "0",
        "imageHost": f"https://{upload_info['upload_host']}" if upload_info.get("upload_host") else "https://www.doubao.com/top/v1",
        "prefix": upload_info.get("upload_path_prefix") or None,
        "useServerCurrentTime": os.environ.get("DOUBAO_UPLOAD_USE_SERVER_TIME", "0").strip().lower() not in {"0", "false", "no", "off"},
        "bundlePath": _uploader_bundle_path(),
    }
    try:
        completed = subprocess.run(
            ["node", helper_path],
            input=json.dumps(request_body, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=150,
            check=False,
        )
    except FileNotFoundError as exc:
        raise APIException(ErrorCode.REQUEST_FAILED, "未找到 Node.js，无法上传豆包参考图") from exc
    except subprocess.TimeoutExpired as exc:
        raise APIException(ErrorCode.TIMEOUT, "豆包参考图上传超时") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise APIException(ErrorCode.REQUEST_FAILED, f"豆包参考图上传失败: {detail[:1000]}")
    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise APIException(ErrorCode.REQUEST_FAILED, f"豆包参考图上传返回异常: {(completed.stdout or '')[:500]}") from exc
    if not isinstance(parsed, dict):
        raise APIException(ErrorCode.REQUEST_FAILED, f"豆包参考图上传返回异常: {parsed}")
    return parsed


def _doubao_uploaded_image_attachment(source: Dict[str, Any], upload_result: Dict[str, Any], url: str) -> Dict[str, Any]:
    file_key = _image_uri_from_upload(upload_result)
    if not file_key:
        raise APIException(ErrorCode.REQUEST_FAILED, f"豆包参考图上传成功但缺少 ImageUri: {upload_result}")
    local_key = str(source.get("localKey") or source.get("identifier") or uuid.uuid4().hex)
    file_name = str(source.get("fileName") or source.get("name") or os.path.basename(str(source.get("local_path") or "image.png")))
    size = int(source.get("size") or _upload_value(upload_result, "fileSize", 0) or 0)
    width = int(source.get("width") or _upload_value(upload_result, "ImageWidth", 0) or 0)
    height = int(source.get("height") or _upload_value(upload_result, "ImageHeight", 0) or 0)
    md5 = str(source.get("md5") or _upload_value(upload_result, "ImageMd5", "") or "")
    image_ori = {
        "url": url or str(source.get("url") or ""),
        "width": width,
        "height": height,
        "format": "",
        "url_formats": {},
    }
    return {
        "type": DOUBAO_IMAGE_ATTACHMENT_TYPE,
        "fileName": file_name,
        "fileKey": file_key,
        "key": file_key,
        "size": size,
        "identifier": local_key,
        "localKey": local_key,
        "parseState": DOUBAO_PARSE_STATE_SUCCESS,
        "reviewState": DOUBAO_REVIEW_STATE_ACCESS,
        "fileType": DOUBAO_ATTACHMENT_FILE_TYPE_OCR_IMAGE,
        "attachmentType": DOUBAO_ATTACHMENT_TYPE_IMAGE_ENUM,
        "url": url or "",
        "blobUrl": url or "",
        "md5": md5,
        "image": {
            "key": file_key,
            "image_ori": image_ori,
        },
        "image_ori": image_ori,
        "uploadResult": upload_result.get("uploadResult") if isinstance(upload_result.get("uploadResult"), dict) else upload_result,
    }


def upload_doubao_image_attachment(cookie_file: str, attachment: Dict[str, Any]) -> Dict[str, Any]:
    upload_info = prepare_doubao_image_upload(cookie_file)
    upload_result = _run_doubao_image_upload_helper(cookie_file, attachment, upload_info)
    file_key = _image_uri_from_upload(upload_result)
    signed_url = get_doubao_file_url(cookie_file, file_key, "image")
    return _doubao_uploaded_image_attachment(attachment, upload_result, signed_url)


def prepare_doubao_attachments(cookie_file: str, attachments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        if item.get("local_path") and not (item.get("fileKey") or item.get("key")):
            log(f"[*] 上传豆包参考图: {os.path.basename(str(item.get('local_path')))}")
            prepared.append(upload_doubao_image_attachment(cookie_file, item))
            continue
        if item.get("key") and not item.get("fileKey"):
            item = {**item, "fileKey": item.get("key")}
        prepared.append(item)
    return prepared


def _looks_like_rate_limit(events: List[Any]) -> bool:
    text = json.dumps(events, ensure_ascii=False).lower()
    return "rate" in text or "频繁" in text or "稍后" in text or "too many" in text


def _looks_like_insufficient(events: List[Any]) -> bool:
    text = json.dumps(events, ensure_ascii=False).lower()
    shortage_markers = (
        "次数不足",
        "额度不足",
        "余额不足",
        "权益不足",
        "insufficient",
        "not enough",
        "exhausted",
        "quota exceeded",
        "11001",
    )
    return any(marker in text for marker in shortage_markers)


def _looks_like_content_block(events: List[Any]) -> bool:
    text = json.dumps(events, ensure_ascii=False).lower()
    return "blocklist" in text or "审核" in text or "risk" in text or "filtered" in text


def submit_video_generation(
    cookie_file: str,
    prompt: str,
    ratio: str = "16:9",
    attachments: Optional[List[Dict[str, Any]]] = None,
    conversation_id: Optional[str] = None,
    camera_movement: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    event = build_samantha_event(
        prompt=prompt,
        ratio=ratio,
        attachments=attachments,
        conversation_id=conversation_id,
        camera_movement=camera_movement,
    )
    event.setdefault("completion_option", {})["event_id"] = "0"
    cookie_name = cookie_record_name(cookie_file)
    rate_limiter.wait_if_needed(cookie_name)
    url = build_url(
        "/samantha/chat/completion",
        params=params,
        include_common=True,
        include_fp=True,
        cookie_file=cookie_file,
    )
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=_cookie_headers(cookie_file, extra={"Agw-Js-Conv": "str"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.submit_timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise classify_http_error(exc.code, _read_error_body(exc)) from exc
    except urllib.error.URLError as exc:
        raise APIException(ErrorCode.REQUEST_FAILED, str(exc)) from exc
    finally:
        rate_limiter.record_request(cookie_name)

    events = parse_sse_text(text)
    if _looks_like_rate_limit(events):
        raise APIException(ErrorCode.RATE_LIMITED, "豆包返回频控/稍后再试")
    if _looks_like_insufficient(events):
        raise APIException(ErrorCode.INSUFFICIENT_CREDITS, "豆包返回次数或额度不足")
    if _looks_like_content_block(events):
        raise APIException(ErrorCode.CONTENT_FILTERED, "豆包返回内容审核/风控拦截")

    vids = extract_vids(events)
    urls = extract_video_urls(events)
    return {
        "status": "submitted",
        "local_message_id": event.get("local_message_id"),
        "local_conversation_id": event.get("local_conversation_id"),
        "conversation_id": event.get("conversation_id"),
        "vids": vids,
        "video_urls": urls,
        "events": events,
        "request_event": event,
    }


def get_play_info(cookie_file: str, vid: str) -> Dict[str, Any]:
    payload = post_json(cookie_file, "/samantha/video/get_play_info", {"vid": vid}, include_fp=False)
    return {"vid": vid, "raw": payload, "video_urls": extract_video_urls(payload)}


def _doubao_response_data_or_raise(payload: Any, action: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    code = payload.get("code")
    if code is not None and str(code) not in {"0", "success", "SUCCESS"}:
        message = payload.get("message") or payload.get("msg") or payload.get("errmsg") or json.dumps(payload, ensure_ascii=False)[:500]
        raise APIException(ErrorCode.REQUEST_FAILED, f"{action}失败: {message}")
    return payload.get("data") if "data" in payload else payload


def create_doubao_media_share(cookie_file: str, message_id: str) -> Dict[str, Any]:
    message_id = str(message_id or "").strip()
    if not message_id:
        raise APIException(ErrorCode.PARAMS_INVALID, "message_id 不能为空")
    payload = post_json(
        cookie_file,
        "https://api-normal.doubao.com/alice/media/bigmusic/share_save",
        {"message_id": message_id},
        include_common=True,
        include_fp=False,
    )
    data = _doubao_response_data_or_raise(payload, "创建分享")
    if not isinstance(data, dict) or not data.get("share_id"):
        raise APIException(ErrorCode.REQUEST_FAILED, f"创建分享失败: {json.dumps(payload, ensure_ascii=False)[:500]}")
    share_id = str(data.get("share_id"))
    return {
        "share_id": share_id,
        "share_url": data.get("share_url") or f"{config.base_url}/video-sharing?share_id={share_id}",
        "raw": payload,
    }


def get_doubao_video_share_info(cookie_file: str, share_id: str, vid: str, creation_id: str = "") -> Dict[str, Any]:
    share_id = str(share_id or "").strip()
    vid = str(vid or "").strip()
    if not share_id or not vid:
        raise APIException(ErrorCode.PARAMS_INVALID, "share_id 和 vid 不能为空")
    payload = post_json(
        cookie_file,
        "/creativity/share/get_video_share_info",
        {"share_id": share_id, "vid": vid, "creation_id": creation_id or ""},
        params={"web_tab_id": str(uuid.uuid4())},
        include_common=True,
        include_fp=False,
        extra_headers={
            "Agw-Js-Conv": "str",
            "X-Tt-Logid": "",
            "Referer": f"{config.base_url}/video-sharing?source_type=mobile&share_id={share_id}&video_id={vid}",
        },
    )
    data = _doubao_response_data_or_raise(payload, "获取分享视频信息")
    if not isinstance(data, dict):
        raise APIException(ErrorCode.REQUEST_FAILED, f"获取分享视频信息失败: {json.dumps(payload, ensure_ascii=False)[:500]}")
    return {"share_id": share_id, "vid": vid, "raw": payload, "data": data}


def _media_url_from_value(value: Any) -> str:
    if isinstance(value, str):
        decoded = _maybe_decode_base64_url(value)
        value = decoded or _normalize_video_url(value)
        return value if value.startswith("http") else ""
    if isinstance(value, dict):
        for key in ("main", "main_url", "mainUrl", "play_url", "playUrl", "video_url", "videoUrl", "url", "download_url", "downloadUrl"):
            result = _media_url_from_value(value.get(key))
            if result:
                return result
    return ""


def _no_watermark_video_url(value: str) -> str:
    url = _normalize_video_url(value)
    for marker in ("video_gen_watermark_dyn", "video_gen_watermark"):
        url = url.replace(marker, "video_gen_no_watermark")
    return url


def _append_unique_url(urls: List[str], value: str) -> None:
    value = _no_watermark_video_url(value)
    if value and value.startswith("http") and value not in urls:
        urls.append(value)


def _video_candidate_from_item(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    main = ""
    backup = ""
    for key in ("main", "main_url", "mainUrl", "play_url", "playUrl", "video_url", "videoUrl", "url", "download_url", "downloadUrl"):
        main = _media_url_from_value(item.get(key))
        if main:
            break
    for key in ("backup", "backup_url", "backupUrl", "backup_url_1", "backupUrl1", "back_url", "backUrl", "fallback_api", "fallbackApi"):
        backup = _media_url_from_value(item.get(key))
        if backup:
            break
    if not main and not backup:
        return None
    return {
        "main": main,
        "backup": backup,
        "width": item.get("width") or item.get("video_width") or item.get("videoWidth"),
        "height": item.get("height") or item.get("video_height") or item.get("videoHeight"),
        "definition": item.get("definition") or item.get("quality") or item.get("resolution"),
    }


def extract_no_watermark_video_info(payload: Any, preferred_url: Optional[str] = None, source: str = "payload") -> Dict[str, Any]:
    urls: List[str] = []
    candidates: List[Dict[str, Any]] = []
    if preferred_url:
        _append_unique_url(urls, preferred_url)

    data = _response_data(payload)
    for key in ("play_infos", "playInfos"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            candidates.extend([candidate for candidate in (_video_candidate_from_item(item) for item in value) if candidate])
    for key in ("play_info", "playInfo", "video", "media"):
        value = data.get(key) if isinstance(data, dict) else None
        candidate = _video_candidate_from_item(value)
        if candidate:
            candidates.append(candidate)
    candidate = _video_candidate_from_item(data)
    if candidate:
        candidates.append(candidate)

    for item in _walk_json(payload):
        candidate = _video_candidate_from_item(item)
        if candidate:
            candidates.append(candidate)

    best_meta: Dict[str, Any] = {}
    seen_pairs: set[Tuple[str, str]] = set()
    for candidate in candidates:
        pair = (candidate.get("main") or "", candidate.get("backup") or "")
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        if not best_meta:
            best_meta = candidate
        _append_unique_url(urls, candidate.get("main") or "")
        _append_unique_url(urls, candidate.get("backup") or "")

    for url in extract_video_urls(payload):
        _append_unique_url(urls, url)

    if not urls:
        return {}

    urls.sort(key=lambda item: 0 if "video_gen_no_watermark" in item else 1)
    return {
        "status": "success",
        "source": source,
        "video_url": urls[0],
        "backup_url": next((url for url in urls[1:] if url != urls[0]), None),
        "video_urls": urls,
        "width": best_meta.get("width"),
        "height": best_meta.get("height"),
        "definition": best_meta.get("definition"),
    }


def _first_text(value: Any) -> str:
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    if isinstance(value, list):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    return ""


def resolve_doubao_no_watermark_video(
    cookie_file: str,
    vid: Optional[str] = None,
    message_id: Optional[str] = None,
    share_id: Optional[str] = None,
    creation_id: str = "",
    video_url: Optional[str] = None,
    raw_payload: Any = None,
) -> Dict[str, Any]:
    fpath = _resolve_cookie_file_path(cookie_file)
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            raw_payload = None

    vids = []
    first_vid = _first_text(vid)
    if first_vid:
        vids.append(first_vid)
    for item in extract_vids(raw_payload):
        if item not in vids:
            vids.append(item)

    fallback = extract_no_watermark_video_info(raw_payload, preferred_url=video_url, source="existing_payload") if (raw_payload is not None or video_url) else {}
    selected_vid = vids[0] if vids else ""
    last_error: Optional[Exception] = None
    share_info: Dict[str, Any] = {}

    try:
        if message_id and not share_id:
            share_info = create_doubao_media_share(fpath, str(message_id))
            share_id = share_info.get("share_id")
        if share_id and selected_vid:
            payload = get_doubao_video_share_info(fpath, str(share_id), selected_vid, creation_id=creation_id)
            resolved = extract_no_watermark_video_info(payload.get("data") or payload, source="share_info")
            if resolved.get("video_url"):
                return {**resolved, "vid": selected_vid, "share_id": share_id, "share_url": share_info.get("share_url")}
    except Exception as exc:
        last_error = exc

    for item_vid in vids:
        try:
            payload = get_play_info(fpath, item_vid)
            preferred = (payload.get("video_urls") or [None])[0]
            resolved = extract_no_watermark_video_info(payload.get("raw") or payload, preferred_url=preferred, source="play_info")
            if resolved.get("video_url"):
                return {**resolved, "vid": item_vid, "share_id": share_id}
        except Exception as exc:
            last_error = exc

    if fallback.get("video_url"):
        return {**fallback, "vid": selected_vid or None, "share_id": share_id or None}

    if last_error:
        if isinstance(last_error, APIException):
            raise last_error
        raise APIException(ErrorCode.REQUEST_FAILED, str(last_error)) from last_error
    raise APIException(ErrorCode.PARAMS_INVALID, "没有找到可解析的视频 vid、message_id 或视频地址")


def download_doubao_no_watermark_video(
    cookie_file: str,
    output_dir: str,
    filename_hint: str = "doubao_video",
    vid: Optional[str] = None,
    message_id: Optional[str] = None,
    share_id: Optional[str] = None,
    creation_id: str = "",
    video_url: Optional[str] = None,
    raw_payload: Any = None,
) -> Dict[str, Any]:
    resolved = resolve_doubao_no_watermark_video(
        cookie_file=cookie_file,
        vid=vid,
        message_id=message_id,
        share_id=share_id,
        creation_id=creation_id,
        video_url=video_url,
        raw_payload=raw_payload,
    )
    width = resolved.get("width")
    height = resolved.get("height")
    size_suffix = f"_{width}x{height}" if width and height else ""
    filename = f"{safe_filename(filename_hint, 'doubao_video')}{size_suffix}_{int(time.time())}.mp4"
    output_path = os.path.join(output_dir, filename)
    urls = [resolved.get("video_url"), resolved.get("backup_url"), *(resolved.get("video_urls") or [])]
    attempted: List[str] = []
    for url in urls:
        if not url or url in attempted:
            continue
        attempted.append(url)
        if download_video(url, output_path):
            return {**resolved, "video_path": output_path, "file_name": filename, "attempted_urls": attempted}
    raise APIException(ErrorCode.REQUEST_FAILED, "无水印地址已解析，但下载失败")


def latest_conversation_threads(cookie_file: str, limit: int = 10) -> List[Dict[str, Any]]:
    payload = post_json(cookie_file, "/samantha/conversation/list", {"index": 0}, include_common=True)
    data = _response_data(payload)
    threads = data.get("thread_list")
    if not isinstance(threads, list):
        return []
    return [thread for thread in threads[:limit] if isinstance(thread, dict)]


def latest_message_payload(cookie_file: str, limit: int = 10) -> Dict[str, Any]:
    request_list: List[Dict[str, Any]] = []
    for thread in latest_conversation_threads(cookie_file, limit=limit):
        conv = thread.get("conversation") if isinstance(thread.get("conversation"), dict) else {}
        conversation_id = str(conv.get("conversation_id") or thread.get("thread_id_str") or thread.get("thread_id") or "")
        if not conversation_id:
            continue
        request_list.append(
            remove_none(
                {
                    "conversation_id": conversation_id,
                    "cursor": "0",
                    "batch_size": 20,
                    "bot_id": conv.get("bot_id") or DOUBAO_DEFAULT_COMMON_PARAMS.get("bot_id"),
                }
            )
        )
    if not request_list:
        return {}
    return post_json(
        cookie_file,
        "/alice/conversation/latest_messagelist",
        {"request_list": request_list},
        include_common=True,
    )


def poll_latest_video_result(cookie_file: str, submitted_after: Optional[float] = None, timeout: Optional[int] = None) -> Dict[str, Any]:
    deadline = time.time() + int(timeout or config.task_timeout)
    last_payload: Any = {}
    min_create_time = int(submitted_after or 0) - 180 if submitted_after else 0
    while time.time() < deadline:
        try:
            payload = latest_message_payload(cookie_file)
            last_payload = payload
            scoped_payload: Any = payload
            if min_create_time:
                recent_items = [
                    item
                    for item in _walk_json(payload)
                    if isinstance(item, dict)
                    and int(item.get("create_time") or 0) >= min_create_time
                    and ("content" in item or "ext" in item)
                ]
                scoped_payload = recent_items
            urls = extract_video_urls(scoped_payload)
            vids = extract_vids(scoped_payload)
            if urls:
                return {"status": "success", "video_urls": urls, "vids": vids, "raw": payload}
        except APIException as exc:
            if exc.code in (ErrorCode.TOKEN_EXPIRED, ErrorCode.CONTENT_FILTERED):
                raise
            last_payload = {"error": exc.message}
        time.sleep(max(1, config.poll_interval))
    raise APIException(ErrorCode.TIMEOUT, f"轮询豆包最新消息视频结果超时: {json.dumps(last_payload, ensure_ascii=False)[:500]}")


def poll_play_info(cookie_file: str, vids: List[str], timeout: Optional[int] = None) -> Dict[str, Any]:
    deadline = time.time() + int(timeout or config.task_timeout)
    last_payload: Dict[str, Any] = {}
    while time.time() < deadline:
        for vid in vids:
            try:
                payload = get_play_info(cookie_file, vid)
                last_payload = payload
                if payload.get("video_urls"):
                    return {"status": "success", **payload}
            except APIException as exc:
                if exc.code in (ErrorCode.TOKEN_EXPIRED, ErrorCode.CONTENT_FILTERED):
                    raise
                last_payload = {"vid": vid, "error": exc.message}
        time.sleep(max(1, config.poll_interval))
    raise APIException(ErrorCode.TIMEOUT, "轮询视频播放地址超时")


def download_video(url: str, output_path: str, timeout: Optional[int] = None) -> bool:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": config.user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout or config.task_timeout) as response:
            with open(output_path, "wb") as f:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
        return os.path.exists(output_path) and os.path.getsize(output_path) > 1024
    except Exception as exc:
        log(f"[WARN] 视频下载失败: {exc}")
        return False


def safe_filename(value: str, fallback: str = "video") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", str(value or "")).strip(" ._")
    return (text or fallback)[:80]


def run_with_cookie(
    prompt: str,
    ratio: str,
    model: str,
    cookie_file: str,
    output_dir: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    conversation_id: Optional[str] = None,
    camera_movement: Optional[str] = None,
    download: bool = True,
    on_task_submitted: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    fpath = _resolve_cookie_file_path(cookie_file)
    cookie_name = cookie_record_name(fpath)
    required = estimate_required_credits(model=model)
    log(f"[*] 使用 Cookie: {os.path.basename(fpath)}")

    try:
        info = get_video_gen_info(fpath)
        credits = info.get("remain_count")
        if credits is not None and int(credits) < required:
            raise APIException(ErrorCode.INSUFFICIENT_CREDITS, f"{credits} < {required}")
    except APIException as exc:
        if exc.code == ErrorCode.TOKEN_EXPIRED:
            update_cookie_record(fpath, credits=read_cookie_credits(fpath), status="disabled", last_error=exc.message)
        raise

    prepared_attachments = prepare_doubao_attachments(fpath, attachments)

    wait_for_cookie_submit_slot(fpath)
    submitted_at = time.time()
    submitted = submit_video_generation(
        cookie_file=fpath,
        prompt=prompt,
        ratio=ratio,
        attachments=prepared_attachments,
        conversation_id=conversation_id,
        camera_movement=camera_movement,
    )
    if on_task_submitted:
        on_task_submitted({"cookie_file": fpath, "cookie_name": cookie_name, **submitted})

    result: Dict[str, Any] = {
        "status": "submitted",
        "cookie_name": cookie_name,
        "cookie_file": os.path.basename(fpath),
        "required_credits": required,
        **submitted,
    }

    if submitted.get("video_urls"):
        result["status"] = "success"
        result["video_url"] = submitted["video_urls"][0]
    elif submitted.get("vids"):
        play_info = poll_play_info(fpath, submitted["vids"])
        result.update(play_info)
        result["video_url"] = play_info.get("video_urls", [None])[0]
    else:
        latest_result = poll_latest_video_result(fpath, submitted_after=submitted_at)
        result.update(latest_result)
        result["video_url"] = latest_result.get("video_urls", [None])[0]

    if download and result.get("video_url"):
        output_dir = output_dir or config.output_dir
        filename = f"{safe_filename(prompt[:30])}_{int(time.time())}.mp4"
        output_path = os.path.join(output_dir, filename)
        if download_video(result["video_url"], output_path):
            result["video_path"] = output_path
        else:
            result["download_error"] = "视频地址已返回，但下载失败"
    if result.get("video_url") or result.get("video_path") or result.get("status") == "success":
        consume_cookie_credits(fpath, required)
    return result


def run(
    prompt: str,
    duration: int = 5,
    ratio: str = "16:9",
    model: str = "doubao-seedance-2.0",
    output_dir: Optional[str] = None,
    cookie_file: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    conversation_id: Optional[str] = None,
    camera_movement: Optional[str] = None,
    download: bool = True,
    on_task_submitted: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    if not str(prompt or "").strip():
        raise APIException(ErrorCode.PARAMS_INVALID, "提示词不能为空")
    if ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
        raise APIException(ErrorCode.PARAMS_INVALID, "比例必须是 16:9、9:16、1:1、4:3 或 3:4")

    if cookie_file:
        if not is_cookie_enabled(cookie_file):
            raise APIException(ErrorCode.PARAMS_INVALID, f"Cookie 已停用: {os.path.basename(cookie_file)}")
        cookie_files = [cookie_file]
    else:
        cookie_files = get_token_files()
    if not cookie_files:
        raise APIException(ErrorCode.PARAMS_INVALID, "没有找到已启用的 Cookie 文件")

    required = estimate_required_credits(model=model, duration=duration)
    refresh_cookie_pool_credits(cookie_files)
    eligible = []
    for item in cookie_files:
        known = read_cookie_credits(item)
        if known is None or int(known) >= max(required, COOKIE_MIN_SUBMIT_CREDITS):
            eligible.append(item)
    if not eligible:
        raise APIException(ErrorCode.INSUFFICIENT_CREDITS, "没有可用次数足够的 Cookie")

    attempts = eligible if cookie_file else get_cookie_attempt_order(eligible)
    last_error: Optional[Exception] = None
    attempted: set[str] = set()

    for _ in range(len(attempts)):
        remaining = [item for item in attempts if _cookie_identity(item) not in attempted]
        if not remaining:
            break
        selected = acquire_cookie_slot(remaining, required)
        attempted.add(_cookie_identity(selected))
        reserved_active = True
        try:
            try:
                result = run_with_cookie(
                    prompt=prompt,
                    ratio=ratio,
                    model=model,
                    cookie_file=selected,
                    output_dir=output_dir,
                    attachments=attachments,
                    conversation_id=conversation_id,
                    camera_movement=camera_movement,
                    download=download,
                    on_task_submitted=on_task_submitted,
                )
                reserved_active = False
                if not cookie_file:
                    mark_cookie_used(cookie_files, selected)
                return result
            except APIException as exc:
                last_error = exc
                if exc.code == ErrorCode.INSUFFICIENT_CREDITS:
                    refresh_cookie_credits(selected, 0)
                    continue
                if exc.code == ErrorCode.RATE_LIMITED:
                    mark_cookie_rate_limited(selected)
                    continue
                if exc.code == ErrorCode.TOKEN_EXPIRED:
                    update_cookie_record(selected, credits=read_cookie_credits(selected), status="disabled", last_error=exc.message)
                    continue
                continue
        finally:
            release_cookie_slot(selected, required if reserved_active else 0)

    if isinstance(last_error, APIException):
        raise last_error
    raise APIException(ErrorCode.VIDEO_FAILED, "所有 Cookie 都执行失败")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Doubao cookie-pool video generation")
    parser.add_argument("--prompt", required=True, help="视频提示词")
    parser.add_argument("--ratio", default="16:9", choices=["16:9", "9:16", "1:1", "4:3", "3:4"])
    parser.add_argument("--model", default="doubao-seedance-2.0")
    parser.add_argument("--cookie-file", default=None)
    parser.add_argument("--cookies", default=config.cookies_dir)
    parser.add_argument("--output", default=config.output_dir)
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()

    config.cookies_dir = args.cookies
    config.output_dir = args.output
    try:
        result = run(
            prompt=args.prompt,
            ratio=args.ratio,
            model=args.model,
            cookie_file=args.cookie_file,
            output_dir=args.output,
            download=not args.no_download,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except APIException as exc:
        print(json.dumps({"error": {"code": exc.code.value[0], "message": exc.message}}, ensure_ascii=False, indent=2))
        raise SystemExit(1)


init_core_database()


if __name__ == "__main__":
    main()
