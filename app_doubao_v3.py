#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Doubao video generation API service.

The service mirrors the Seedance cookie-pool shape while keeping Doubao-specific
HTTP details inside doubao_v3.py.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_file
from werkzeug.utils import secure_filename

from doubao_v3 import (
    APIException,
    DB_PATH,
    ErrorCode,
    config,
    cookie_record_name,
    download_doubao_no_watermark_video,
    estimate_required_credits,
    extract_video_urls,
    get_cookie_record_status,
    get_db_connection,
    get_token_files,
    init_core_database,
    load_cookies,
    load_runtime_config,
    log,
    normalize_cookie_filename,
    read_cookie_credits,
    runtime_config_diagnostics,
    run as doubao_run,
    save_runtime_config,
    resolve_doubao_no_watermark_video,
    test_cookie_file,
    update_cookie_record,
    update_cookie_status,
)


SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get("DOUBAO_RUNTIME_DIR", SOURCE_DIR)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
COOKIES_DIR = os.environ.get("COOKIES_DIR", os.path.join(BASE_DIR, "cookies"))
UPLOAD_FOLDER = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
OUTPUT_FOLDER = os.environ.get("OUTPUT_DIR", os.path.join(BASE_DIR, "downloads"))
TASK_FILES_DIR = os.path.join(DATA_DIR, "doubao-task-files")

config.runtime_dir = BASE_DIR
config.data_dir = DATA_DIR
config.cookies_dir = COOKIES_DIR
config.output_dir = OUTPUT_FOLDER

for folder in (DATA_DIR, COOKIES_DIR, UPLOAD_FOLDER, OUTPUT_FOLDER, TASK_FILES_DIR):
    os.makedirs(folder, exist_ok=True)

MAX_WORKERS = max(1, min(int(os.environ.get("MAX_WORKERS", "3")), 20))
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(50 * 1024 * 1024)))
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
    return response


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUBMITTED = "submitted"
    SUCCESS = "success"
    FAILED = "failed"


def allowed_image_file(filename: str) -> bool:
    suffix = str(filename or "").rsplit(".", 1)
    return len(suffix) == 2 and suffix[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def read_image_dimensions(path: str) -> tuple[int, int]:
    try:
        with open(path, "rb") as f:
            header = f.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
            if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
                f.seek(12)
                while True:
                    chunk = f.read(8)
                    if len(chunk) < 8:
                        break
                    ctype = chunk[:4]
                    size = int.from_bytes(chunk[4:8], "little")
                    data = f.read(size + (size % 2))
                    if ctype == b"VP8X" and len(data) >= 10:
                        width = 1 + int.from_bytes(data[4:7], "little")
                        height = 1 + int.from_bytes(data[7:10], "little")
                        return width, height
                    if ctype == b"VP8 " and len(data) >= 10:
                        return int.from_bytes(data[6:8], "little") & 0x3FFF, int.from_bytes(data[8:10], "little") & 0x3FFF
                    if ctype == b"VP8L" and len(data) >= 5:
                        bits = int.from_bytes(data[1:5], "little")
                        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if header.startswith(b"\xff\xd8"):
                f.seek(2)
                while True:
                    marker_start = f.read(1)
                    if not marker_start:
                        break
                    if marker_start != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if marker in {b"\xc0", b"\xc1", b"\xc2", b"\xc3", b"\xc5", b"\xc6", b"\xc7", b"\xc9", b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"}:
                        segment = f.read(7)
                        if len(segment) >= 7:
                            return int.from_bytes(segment[3:5], "big"), int.from_bytes(segment[5:7], "big")
                        break
                    length_bytes = f.read(2)
                    if len(length_bytes) < 2:
                        break
                    length = int.from_bytes(length_bytes, "big")
                    f.seek(max(0, length - 2), os.SEEK_CUR)
    except Exception:
        pass
    return 0, 0


def save_uploaded_reference_files() -> List[Dict[str, Any]]:
    attachments: List[Dict[str, Any]] = []
    incoming = []
    for field_name in ("files", "images"):
        incoming.extend(request.files.getlist(field_name))
    for index, file in enumerate(incoming):
        if not file or not file.filename:
            continue
        if not allowed_image_file(file.filename):
            raise APIException(ErrorCode.PARAMS_INVALID, f"参考图只支持 png/jpg/jpeg/webp: {file.filename}")
        safe_name = secure_filename(file.filename) or f"image_{index}.png"
        filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_name}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        width, height = read_image_dimensions(filepath)
        attachments.append(
            {
                "type": "image",
                "fileName": safe_name,
                "local_path": filepath,
                "size": os.path.getsize(filepath),
                "mime": file.mimetype or "",
                "width": width,
                "height": height,
                "from": "local-upload",
            }
        )
    return attachments


def materialize_task_attachments(attachments: List[Dict[str, Any]], output_dir: str) -> List[Dict[str, Any]]:
    materialized: List[Dict[str, Any]] = []
    for index, item in enumerate(attachments or []):
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        local_path = str(next_item.get("local_path") or "")
        if local_path and os.path.exists(local_path):
            filename = f"ref_{index}_{os.path.basename(local_path)}"
            target_path = os.path.join(output_dir, filename)
            if os.path.abspath(local_path) != os.path.abspath(target_path):
                shutil.copy2(local_path, target_path)
            next_item["source_path"] = local_path
            next_item["local_path"] = target_path
        materialized.append(next_item)
    return materialized


@dataclass
class Task:
    task_id: str
    prompt: str
    ratio: str = "16:9"
    model: str = "doubao-seedance-2.0"
    duration: int = 10
    output_dir: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    cookie_file: Optional[str] = None
    cookie_name: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    video_path: Optional[str] = None
    video_url: Optional[str] = None
    error_message: Optional[str] = None
    raw_result: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        with self.lock:
            result = {
                "task_id": self.task_id,
                "prompt": self.prompt,
                "ratio": self.ratio,
                "model": self.model,
                "duration": self.duration,
                "status": self.status.value,
                "progress": self.progress,
                "attachments_count": len(self.attachments),
                "cookie_name": self.cookie_name,
                "cookie_file": os.path.basename(self.cookie_file) if self.cookie_file else None,
                "video_path": self.video_path,
                "video_url": self.video_url,
                "error_message": self.error_message,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            }
            if include_raw and self.raw_result is not None:
                result["raw_result"] = self.raw_result
            if self.status == TaskStatus.SUCCESS and self.video_path:
                result["download_url"] = f"/api/video/{self.task_id}"
            return result

    def to_openai_dict(self) -> Dict[str, Any]:
        status_map = {
            TaskStatus.PENDING: "queued",
            TaskStatus.RUNNING: "processing",
            TaskStatus.SUBMITTED: "processing",
            TaskStatus.SUCCESS: "succeeded",
            TaskStatus.FAILED: "failed",
        }
        result: Dict[str, Any] = {
            "created": int(self.created_at.timestamp()) if self.created_at else int(time.time()),
            "task_id": self.task_id,
            "status": status_map.get(self.status, self.status.value),
        }
        if self.status == TaskStatus.SUCCESS:
            url = f"/api/video/{self.task_id}" if self.video_path else self.video_url
            result["data"] = [{"url": url, "revised_prompt": self.prompt}]
        if self.status == TaskStatus.FAILED:
            result["error"] = self.error_message or "unknown error"
        return result


def init_database() -> None:
    init_core_database()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            prompt TEXT NOT NULL,
            ratio TEXT NOT NULL,
            model TEXT NOT NULL,
            duration INTEGER NOT NULL,
            output_dir TEXT NOT NULL,
            attachments TEXT,
            cookie_file TEXT,
            cookie_name TEXT,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            video_path TEXT,
            video_url TEXT,
            error_message TEXT,
            raw_result TEXT,
            created_at TEXT,
            started_at TEXT,
            completed_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def task_file_path(task_id: str) -> str:
    return os.path.join(TASK_FILES_DIR, f"{task_id}.json")


def json_dumps_safe(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


def save_task_to_db(task: Task) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    with task.lock:
        cursor.execute(
            """
            INSERT OR REPLACE INTO tasks
                (task_id, prompt, ratio, model, duration, output_dir, attachments,
                 cookie_file, cookie_name, status, progress, video_path, video_url,
                 error_message, raw_result, created_at, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.prompt,
                task.ratio,
                task.model,
                task.duration,
                task.output_dir,
                json_dumps_safe(task.attachments),
                os.path.basename(task.cookie_file) if task.cookie_file else None,
                task.cookie_name,
                task.status.value,
                task.progress,
                task.video_path,
                task.video_url,
                task.error_message,
                json_dumps_safe(task.raw_result) if task.raw_result is not None else None,
                task.created_at.isoformat() if task.created_at else None,
                task.started_at.isoformat() if task.started_at else None,
                task.completed_at.isoformat() if task.completed_at else None,
            ),
        )
        snapshot = task.to_dict(include_raw=True)
    conn.commit()
    conn.close()
    with open(task_file_path(task.task_id), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_task_from_row(row: sqlite3.Row) -> Task:
    try:
        attachments = json.loads(row["attachments"] or "[]")
    except json.JSONDecodeError:
        attachments = []
    try:
        raw_result = json.loads(row["raw_result"]) if row["raw_result"] else None
    except json.JSONDecodeError:
        raw_result = None

    task = Task(
        task_id=row["task_id"],
        prompt=row["prompt"],
        ratio=row["ratio"],
        model=row["model"],
        duration=int(row["duration"] or 10),
        output_dir=row["output_dir"],
        attachments=attachments if isinstance(attachments, list) else [],
        cookie_file=row["cookie_file"],
        cookie_name=row["cookie_name"],
        raw_result=raw_result,
    )
    task.status = TaskStatus(row["status"])
    task.progress = int(row["progress"] or 0)
    task.video_path = row["video_path"]
    task.video_url = row["video_url"]
    task.error_message = row["error_message"]
    task.created_at = parse_datetime(row["created_at"]) or datetime.now()
    task.started_at = parse_datetime(row["started_at"])
    task.completed_at = parse_datetime(row["completed_at"])
    return task


class AsyncTaskManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self.tasks: Dict[str, Task] = {}
        self.events: Dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def add_task(
        self,
        prompt: str,
        ratio: str,
        model: str,
        duration: int,
        attachments: Optional[List[Dict[str, Any]]] = None,
        cookie_file: Optional[str] = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        output_dir = os.path.join(OUTPUT_FOLDER, task_id)
        os.makedirs(output_dir, exist_ok=True)
        task_attachments = materialize_task_attachments(attachments or [], output_dir)
        task = Task(
            task_id=task_id,
            prompt=prompt,
            ratio=ratio,
            model=model,
            duration=duration,
            output_dir=output_dir,
            attachments=task_attachments,
            cookie_file=cookie_file,
        )
        with self.lock:
            self.tasks[task_id] = task
            self.events[task_id] = threading.Event()
        save_task_to_db(task)
        self.executor.submit(self._execute_task, task_id)
        return task_id

    def _execute_task(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if not task:
            return
        with task.lock:
            task.status = TaskStatus.RUNNING
            task.progress = 10
            task.started_at = datetime.now()
            task.error_message = None
        save_task_to_db(task)

        def on_submitted(info: Dict[str, Any]) -> None:
            with task.lock:
                task.cookie_file = info.get("cookie_file") or task.cookie_file
                task.cookie_name = info.get("cookie_name") or task.cookie_name
                task.status = TaskStatus.SUBMITTED
                task.progress = max(task.progress, 35)
                task.raw_result = {"submitted": info}
            save_task_to_db(task)

        try:
            result = doubao_run(
                prompt=task.prompt,
                duration=task.duration,
                ratio=task.ratio,
                model=task.model,
                output_dir=task.output_dir,
                cookie_file=task.cookie_file,
                attachments=task.attachments,
                download=True,
                on_task_submitted=on_submitted,
            )
            with task.lock:
                task.raw_result = result
                task.cookie_name = result.get("cookie_name") or task.cookie_name
                task.cookie_file = result.get("cookie_file") or task.cookie_file
                task.video_path = result.get("video_path")
                task.video_url = result.get("video_url")
                if result.get("status") == "submitted_without_vid":
                    task.status = TaskStatus.SUBMITTED
                    task.progress = max(task.progress, 45)
                elif task.video_path or task.video_url or result.get("status") == "success":
                    task.status = TaskStatus.SUCCESS
                    task.progress = 100
                    task.completed_at = datetime.now()
                else:
                    task.status = TaskStatus.SUBMITTED
                    task.progress = max(task.progress, 60)
            save_task_to_db(task)
        except APIException as exc:
            with task.lock:
                task.status = TaskStatus.FAILED
                task.progress = 100
                task.error_message = exc.message
                task.completed_at = datetime.now()
            save_task_to_db(task)
        except Exception as exc:
            with task.lock:
                task.status = TaskStatus.FAILED
                task.progress = 100
                task.error_message = str(exc)
                task.completed_at = datetime.now()
            save_task_to_db(task)
        finally:
            with self.lock:
                event = self.events.get(task_id)
            if event:
                event.set()

    def get_task(self, task_id: str) -> Optional[Task]:
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                return task
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        task = load_task_from_row(row)
        with self.lock:
            self.tasks[task_id] = task
        return task

    def get_all_tasks(self, limit: int = 100, offset: int = 0, status: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if status:
            cursor.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
        rows = cursor.fetchall()
        conn.close()
        results = []
        for row in rows:
            with self.lock:
                task = self.tasks.get(row["task_id"])
            if not task:
                task = load_task_from_row(row)
            results.append(task.to_dict())
        return results

    def get_running_count(self) -> int:
        with self.lock:
            return sum(1 for task in self.tasks.values() if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.SUBMITTED))

    def wait_for_task(self, task_id: str, timeout: int) -> Optional[Task]:
        with self.lock:
            event = self.events.setdefault(task_id, threading.Event())
        event.wait(timeout=max(1, timeout))
        return self.get_task(task_id)


init_database()
task_manager = AsyncTaskManager()


def error_response(code: ErrorCode, detail: str = ""):
    return jsonify({"error": {"code": code.value[0], "message": f"{code.value[1]}: {detail}" if detail else code.value[1], "type": code.name}}), code.value[2]


def parse_enabled_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        raise ValueError("缺少 enabled 参数")
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled", "inactive", "paused"}


def safe_resolve_runtime(file_path: str) -> Optional[str]:
    if not file_path:
        return None
    allowed_roots = [Path(BASE_DIR).resolve(), Path(OUTPUT_FOLDER).resolve(), Path(UPLOAD_FOLDER).resolve()]
    path_value = Path(file_path)
    full_path = path_value.resolve() if path_value.is_absolute() else Path(BASE_DIR, file_path).resolve()
    for root in allowed_roots:
        try:
            full_path.relative_to(root)
            return str(full_path)
        except ValueError:
            continue
    return None


def read_request_json() -> Dict[str, Any]:
    if request.is_json:
        return request.get_json(silent=True) or {}
    if request.form:
        data = dict(request.form.items())
        if "attachments" in data:
            try:
                data["attachments"] = json.loads(data["attachments"])
            except json.JSONDecodeError:
                data["attachments"] = []
        return data
    return {}


def media_result_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "status",
        "source",
        "vid",
        "share_id",
        "share_url",
        "video_url",
        "backup_url",
        "video_urls",
        "width",
        "height",
        "definition",
        "video_path",
        "file_name",
        "attempted_urls",
    )
    return {key: result.get(key) for key in keys if key in result and result.get(key) is not None}


def resolve_media_cookie_file(data: Dict[str, Any], task: Optional[Task] = None) -> str:
    raw_cookie = (
        data.get("cookie_file")
        or data.get("cookie")
        or (task.cookie_file if task else None)
        or ((task.raw_result or {}).get("cookie_file") if task and isinstance(task.raw_result, dict) else None)
    )
    if raw_cookie:
        text = str(raw_cookie)
        if os.path.isabs(text) and os.path.exists(text):
            return text
        candidates = [
            os.path.join(COOKIES_DIR, os.path.basename(text)),
            os.path.join(COOKIES_DIR, normalize_cookie_filename(text)),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        raise APIException(ErrorCode.PARAMS_INVALID, f"Cookie 文件不存在: {os.path.basename(text)}")

    token_files = get_token_files()
    if not token_files:
        raise APIException(ErrorCode.PARAMS_INVALID, "没有找到已启用的 Cookie 文件")
    return os.path.join(COOKIES_DIR, token_files[0])


def first_video_url_from_task(task: Task, data: Dict[str, Any]) -> str:
    direct = str(data.get("video_url") or "").strip()
    if direct:
        return direct
    if task.video_url:
        return task.video_url
    raw = task.raw_result if isinstance(task.raw_result, dict) else {}
    for key in ("video_url",):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("video_urls",):
        value = raw.get(key)
        if isinstance(value, list) and value:
            return str(value[0])
    submitted = raw.get("submitted") if isinstance(raw.get("submitted"), dict) else {}
    if isinstance(submitted.get("video_url"), str) and submitted.get("video_url"):
        return submitted["video_url"]
    if isinstance(submitted.get("video_urls"), list) and submitted.get("video_urls"):
        return str(submitted["video_urls"][0])
    urls = extract_video_urls(task.raw_result)
    return urls[0] if urls else ""


def first_text_from_request(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def safe_watermark_file_path(relative_path: str) -> Optional[str]:
    base = Path(OUTPUT_FOLDER).resolve()
    full_path = Path(OUTPUT_FOLDER, relative_path).resolve()
    try:
        full_path.relative_to(base)
    except ValueError:
        return None
    return str(full_path)


@app.route("/api/health", methods=["GET"])
def health_check():
    runtime = runtime_config_diagnostics()
    return jsonify(
        {
            "status": "healthy",
            "service": "doubao-cookie-pool-video",
            "version": "0.1.0",
            "cookies_count": len(get_token_files()),
            "max_workers": MAX_WORKERS,
            "running_tasks": task_manager.get_running_count(),
            "db_path": DB_PATH,
            "runtime_ready": runtime["ready"],
            "runtime_missing": runtime["missing"],
        }
    )


@app.route("/api/runtime-config", methods=["GET"])
def get_runtime_config_api():
    diag = runtime_config_diagnostics()
    cfg = load_runtime_config()
    return jsonify(
        {
            "status": "success",
            "config": {
                "common_params": diag["common_params"],
                "fp": diag["fp"],
                "updated_at": cfg.get("updated_at"),
                "config_path": diag["config_path"],
            },
            "diagnostics": {
                "ready": diag["ready"],
                "missing": diag["missing"],
                "params_count": diag["params_count"],
                "has_fp": diag["has_fp"],
            },
        }
    )


@app.route("/api/runtime-config", methods=["POST"])
def update_runtime_config_api():
    try:
        data = request.json or {}
        common_params = data.get("common_params", None)
        fp = data.get("fp", None)
        if isinstance(common_params, str):
            text = common_params.strip()
            if text.startswith("http://") or text.startswith("https://"):
                from urllib.parse import parse_qsl, urlsplit

                common_params = dict(parse_qsl(urlsplit(text).query, keep_blank_values=True))
            elif text:
                common_params = json.loads(text)
            else:
                common_params = {}
        if common_params is not None and not isinstance(common_params, dict):
            return jsonify({"status": "error", "message": "common_params 必须是 JSON 对象或完整 URL"}), 400
        save_runtime_config(common_params=common_params, fp=fp)
        return get_runtime_config_api()
    except json.JSONDecodeError as exc:
        return jsonify({"status": "error", "message": f"运行时参数 JSON 格式错误: {exc}"}), 400
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/cookies", methods=["GET"])
def list_cookies():
    cookies_files = get_token_files(active_only=False)
    rows_by_name: Dict[str, Dict[str, Any]] = {}
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM cookies")
        for row in cursor.fetchall():
            rows_by_name[cookie_record_name(row["name"])] = dict(row)
    finally:
        conn.close()

    cookies = []
    for index, filename in enumerate(cookies_files):
        name = cookie_record_name(filename)
        path = os.path.join(COOKIES_DIR, filename)
        row = rows_by_name.get(name, {})
        status = row.get("status") or "active"
        cookies.append(
            {
                "id": index + 1,
                "name": name,
                "filename": filename,
                "path": path,
                "size": os.path.getsize(path) if os.path.exists(path) else 0,
                "credits": row.get("remain_count", row.get("credits")),
                "remain_count": row.get("remain_count", row.get("credits")),
                "has_generating_task": bool(row.get("has_generating_task") or 0),
                "is_beta_user": bool(row.get("is_beta_user") or 0),
                "last_used": row.get("last_used"),
                "last_error": row.get("last_error"),
                "status": status,
                "enabled": str(status).lower() not in {"disabled", "inactive", "off", "paused"},
            }
        )
    return jsonify({"status": "success", "cookies": cookies, "count": len(cookies)})


@app.route("/api/cookies", methods=["POST"])
def upload_cookie():
    try:
        name = request.form.get("name", "").strip()
        content = None
        save_path = None
        if "file" in request.files:
            file = request.files["file"]
            if not file or not file.filename:
                return jsonify({"status": "error", "message": "请上传 Cookie JSON 文件"}), 400
            filename = normalize_cookie_filename(name or secure_filename(file.filename) or file.filename)
            save_path = os.path.join(COOKIES_DIR, filename)
            file.save(save_path)
            with open(save_path, "r", encoding="utf-8") as f:
                content = json.load(f)
        elif request.is_json and "content" in (request.json or {}):
            payload = request.json or {}
            raw_content = payload.get("content")
            if isinstance(raw_content, str):
                content = json.loads(raw_content)
            else:
                content = raw_content
            filename = normalize_cookie_filename(str(payload.get("name") or name or "cookie"))
            save_path = os.path.join(COOKIES_DIR, filename)
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            return jsonify({"status": "error", "message": "请上传文件或提供 JSON content"}), 400

        try:
            load_cookies(save_path)
        except Exception:
            if save_path and os.path.exists(save_path):
                os.remove(save_path)
            raise

        update_cookie_record(save_path, credits=read_cookie_credits(save_path), status="active")
        return jsonify({"status": "success", "message": f"Cookie {os.path.basename(save_path)} 上传成功", "filename": os.path.basename(save_path)})
    except json.JSONDecodeError:
        return jsonify({"status": "error", "message": "Cookie 文件必须是有效 JSON"}), 400
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/cookies/<path:cookie_name>/status", methods=["POST", "PATCH", "OPTIONS"])
def update_cookie_status_api(cookie_name):
    if request.method == "OPTIONS":
        return jsonify({"status": "success"})
    try:
        data = request.json or {}
        enabled = data.get("enabled")
        if enabled is None and "status" in data:
            enabled = str(data.get("status")).strip().lower() not in {"disabled", "inactive", "off", "paused"}
        result = update_cookie_status(cookie_name, parse_enabled_flag(enabled))
        return jsonify({"status": "success", "cookie": result})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/cookies/<path:cookie_name>", methods=["DELETE", "OPTIONS"])
def delete_cookie(cookie_name):
    if request.method == "OPTIONS":
        return jsonify({"status": "success"})
    filename = normalize_cookie_filename(cookie_name)
    path = os.path.join(COOKIES_DIR, filename)
    try:
        if os.path.exists(path):
            os.remove(path)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cookies WHERE name IN (?, ?)", (cookie_record_name(filename), filename))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Cookie 已删除"})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/cookies/<path:cookie_name>/test", methods=["GET", "POST", "OPTIONS"])
def test_cookie(cookie_name):
    if request.method == "OPTIONS":
        return jsonify({"status": "success"})
    try:
        filename = normalize_cookie_filename(cookie_name)
        path = os.path.join(COOKIES_DIR, filename)
        if not os.path.exists(path):
            return jsonify({"status": "error", "message": "Cookie 文件不存在"}), 404
        result = test_cookie_file(path)
        return jsonify(result)
    except APIException as exc:
        return jsonify({"status": "error", "message": exc.message, "code": exc.code.name}), exc.code.value[2]
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/cookies/check-all", methods=["POST"])
def check_all_cookies():
    results = []
    for filename in get_token_files(active_only=False):
        try:
            result = test_cookie_file(os.path.join(COOKIES_DIR, filename))
            result["filename"] = filename
            results.append(result)
        except Exception as exc:
            results.append({"status": "failed", "filename": filename, "name": cookie_record_name(filename), "error": str(exc)})
    return jsonify({"status": "success", "results": results, "count": len(results)})


@app.route("/api/watermark/resolve", methods=["POST"])
def resolve_no_watermark_video_api():
    try:
        data = read_request_json()
        cookie_file = resolve_media_cookie_file(data)
        result = resolve_doubao_no_watermark_video(
            cookie_file=cookie_file,
            vid=first_text_from_request(data, "vid"),
            message_id=first_text_from_request(data, "message_id"),
            share_id=first_text_from_request(data, "share_id"),
            creation_id=first_text_from_request(data, "creation_id"),
            video_url=first_text_from_request(data, "video_url"),
            raw_payload=data.get("raw_payload") or data.get("raw"),
        )
        return jsonify({"status": "success", "media": media_result_payload(result)})
    except APIException as exc:
        return error_response(exc.code, exc.detail)
    except Exception as exc:
        return error_response(ErrorCode.REQUEST_FAILED, str(exc))


@app.route("/api/watermark/download", methods=["POST"])
def download_no_watermark_video_api():
    try:
        data = read_request_json()
        cookie_file = resolve_media_cookie_file(data)
        output_dir = os.path.join(OUTPUT_FOLDER, "watermark")
        os.makedirs(output_dir, exist_ok=True)
        result = download_doubao_no_watermark_video(
            cookie_file=cookie_file,
            output_dir=output_dir,
            filename_hint=str(data.get("filename") or data.get("title") or "doubao_video"),
            vid=first_text_from_request(data, "vid"),
            message_id=first_text_from_request(data, "message_id"),
            share_id=first_text_from_request(data, "share_id"),
            creation_id=first_text_from_request(data, "creation_id"),
            video_url=first_text_from_request(data, "video_url"),
            raw_payload=data.get("raw_payload") or data.get("raw"),
        )
        media = media_result_payload(result)
        relative_path = os.path.relpath(result["video_path"], OUTPUT_FOLDER).replace(os.sep, "/")
        media["download_url"] = f"/api/watermark/file/{relative_path}"
        return jsonify({"status": "success", "media": media})
    except APIException as exc:
        return error_response(exc.code, exc.detail)
    except Exception as exc:
        return error_response(ErrorCode.REQUEST_FAILED, str(exc))


@app.route("/api/watermark/file/<path:relative_path>", methods=["GET"])
def get_watermark_video_file(relative_path):
    path = safe_watermark_file_path(relative_path)
    if not path or not os.path.exists(path):
        return jsonify({"status": "error", "message": "视频文件不存在"}), 404
    return send_file(path, mimetype="video/mp4", as_attachment=True)


@app.route("/api/task/<task_id>/download-original", methods=["POST"])
def download_task_original_video(task_id):
    try:
        task = task_manager.get_task(task_id)
        if not task:
            return jsonify({"status": "error", "message": "任务不存在"}), 404
        data = read_request_json()
        cookie_file = resolve_media_cookie_file(data, task)
        output_dir = task.output_dir or os.path.join(OUTPUT_FOLDER, task.task_id)
        os.makedirs(output_dir, exist_ok=True)
        result = download_doubao_no_watermark_video(
            cookie_file=cookie_file,
            output_dir=output_dir,
            filename_hint=str(data.get("filename") or task.prompt[:30] or task.task_id),
            vid=first_text_from_request(data, "vid"),
            message_id=first_text_from_request(data, "message_id"),
            share_id=first_text_from_request(data, "share_id"),
            creation_id=first_text_from_request(data, "creation_id"),
            video_url=first_video_url_from_task(task, data),
            raw_payload=data.get("raw_payload") or data.get("raw") or task.raw_result,
        )
        media = media_result_payload(result)
        media["download_url"] = f"/api/video/{task.task_id}"

        with task.lock:
            previous_raw = task.raw_result if isinstance(task.raw_result, dict) else {}
            task.raw_result = {
                **previous_raw,
                "no_watermark_download": {
                    **media,
                    "downloaded_at": datetime.now().isoformat(),
                },
            }
            task.video_path = result.get("video_path")
            task.video_url = result.get("video_url") or task.video_url
            task.status = TaskStatus.SUCCESS
            task.progress = 100
            task.error_message = None
            task.completed_at = task.completed_at or datetime.now()
        save_task_to_db(task)
        event = task_manager.events.get(task.task_id)
        if event:
            event.set()
        return jsonify({"status": "success", "media": media, "task": task.to_dict(include_raw=False)})
    except APIException as exc:
        return error_response(exc.code, exc.detail)
    except Exception as exc:
        return error_response(ErrorCode.REQUEST_FAILED, str(exc))


def submit_generation_from_request(openai_compatible: bool = False):
    try:
        data = read_request_json()
        prompt = str(data.get("prompt") or data.get("input") or "").strip()
        ratio = str(data.get("ratio") or data.get("aspect_ratio") or "16:9").strip()
        model = str(data.get("model") or "doubao-seedance-2.0").strip()
        duration = int(data.get("duration") or 10)
        cookie_file = data.get("cookie_file") or data.get("cookie")
        attachments = data.get("attachments") or []
        if isinstance(attachments, str):
            attachments = json.loads(attachments)
        if not isinstance(attachments, list):
            return error_response(ErrorCode.PARAMS_INVALID, "attachments 必须是数组")
        if not prompt:
            return error_response(ErrorCode.PARAMS_INVALID, "提示词不能为空")
        if ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
            return error_response(ErrorCode.PARAMS_INVALID, "比例必须是 16:9、9:16、1:1、4:3 或 3:4")
        uploaded_attachments = save_uploaded_reference_files()
        attachments = attachments + uploaded_attachments

        task_id = task_manager.add_task(
            prompt=prompt,
            ratio=ratio,
            model=model,
            duration=duration,
            attachments=attachments,
            cookie_file=cookie_file,
        )
        response = {
            "created": int(time.time()),
            "task_id": task_id,
            "status": "processing",
            "required_credits": estimate_required_credits(model=model, duration=duration),
            "message": f"任务已提交，请使用 GET /v1/videos/generations/{task_id} 查询结果",
        }
        return jsonify(response)
    except APIException as exc:
        return error_response(exc.code, exc.detail)
    except Exception as exc:
        return error_response(ErrorCode.REQUEST_FAILED, str(exc))


@app.route("/v1/models", methods=["GET"])
def list_models():
    return jsonify(
        {
            "object": "list",
            "data": [
                {"id": "doubao-seedance-2.0", "object": "model", "created": 1700000000, "owned_by": "doubao"},
                {"id": "doubao-video-generation", "object": "model", "created": 1700000000, "owned_by": "doubao"},
            ],
        }
    )


@app.route("/v1/videos/generations", methods=["POST"])
def generate_video_openai():
    return submit_generation_from_request(openai_compatible=True)


@app.route("/v1/videos/generations/async", methods=["POST"])
def generate_video_async():
    return submit_generation_from_request(openai_compatible=True)


@app.route("/v1/videos/generations/<task_id>", methods=["GET"])
def get_video_status_openai(task_id):
    if task_id == "async":
        return error_response(ErrorCode.PARAMS_INVALID, "请使用 POST /v1/videos/generations/async 提交任务")
    task = task_manager.get_task(task_id)
    if not task:
        return error_response(ErrorCode.PARAMS_INVALID, "任务不存在")
    return jsonify(task.to_openai_dict())


@app.route("/v1/videos/generations/async/<task_id>", methods=["GET"])
def wait_video_status_openai(task_id):
    timeout = int(request.args.get("timeout", "300"))
    task = task_manager.wait_for_task(task_id, timeout=timeout)
    if not task:
        return error_response(ErrorCode.TIMEOUT, "查询超时")
    return jsonify(task.to_openai_dict())


@app.route("/api/generate-video", methods=["POST"])
def generate_video_legacy():
    return submit_generation_from_request(openai_compatible=False)


@app.route("/api/task/<task_id>", methods=["GET"])
def get_task_status(task_id):
    include_raw = str(request.args.get("raw", "")).lower() in {"1", "true", "yes"}
    task = task_manager.get_task(task_id)
    if not task:
        return jsonify({"status": "error", "message": "任务不存在"}), 404
    return jsonify(task.to_dict(include_raw=include_raw))


@app.route("/api/task/<task_id>/retry", methods=["POST"])
def retry_task(task_id):
    try:
        task = task_manager.get_task(task_id)
        if not task:
            return jsonify({"status": "error", "message": "任务不存在"}), 404
        if task.status != TaskStatus.FAILED:
            return error_response(ErrorCode.PARAMS_INVALID, "只有失败任务可以重新提交")

        with task.lock:
            try:
                attachments = json.loads(json.dumps(task.attachments, ensure_ascii=False))
            except TypeError:
                attachments = [dict(item) for item in task.attachments if isinstance(item, dict)]
            new_task_id = task_manager.add_task(
                prompt=task.prompt,
                ratio=task.ratio,
                model=task.model,
                duration=task.duration,
                attachments=attachments,
                cookie_file=None,
            )
        return jsonify(
            {
                "status": "success",
                "task_id": new_task_id,
                "source_task_id": task_id,
                "message": "任务已重新提交到 Cookie 池",
            }
        )
    except APIException as exc:
        return error_response(exc.code, exc.detail)
    except Exception as exc:
        return error_response(ErrorCode.REQUEST_FAILED, str(exc))


@app.route("/api/task/<task_id>/rerun", methods=["POST"])
def rerun_task(task_id):
    try:
        task = task_manager.get_task(task_id)
        if not task:
            return jsonify({"status": "error", "message": "任务不存在"}), 404

        with task.lock:
            try:
                attachments = json.loads(json.dumps(task.attachments, ensure_ascii=False))
            except TypeError:
                attachments = [dict(item) for item in task.attachments if isinstance(item, dict)]
            new_task_id = task_manager.add_task(
                prompt=task.prompt,
                ratio=task.ratio,
                model=task.model,
                duration=task.duration,
                attachments=attachments,
                cookie_file=task.cookie_file,
            )
        return jsonify(
            {
                "status": "success",
                "task_id": new_task_id,
                "source_task_id": task_id,
                "message": "任务已重新提交到 Cookie 池",
            }
        )
    except APIException as exc:
        return error_response(exc.code, exc.detail)
    except Exception as exc:
        return error_response(ErrorCode.REQUEST_FAILED, str(exc))


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    limit = max(1, min(int(request.args.get("limit", "100")), 500))
    offset = max(0, int(request.args.get("offset", "0")))
    status = request.args.get("status")
    tasks = task_manager.get_all_tasks(limit=limit, offset=offset, status=status)
    return jsonify({"status": "success", "tasks": tasks, "count": len(tasks), "running_count": task_manager.get_running_count()})


@app.route("/api/video/<task_id>", methods=["GET"])
def get_video(task_id):
    task = task_manager.get_task(task_id)
    if not task:
        return jsonify({"status": "error", "message": "任务不存在"}), 404
    if not task.video_path:
        return jsonify({"status": "error", "message": "任务没有本地视频文件"}), 404
    path = safe_resolve_runtime(task.video_path)
    if not path or not os.path.exists(path):
        return jsonify({"status": "error", "message": "视频文件不存在"}), 404
    return send_file(path, mimetype="video/mp4", as_attachment=True)


@app.route("/api/tasks/clear", methods=["POST"])
def clear_tasks():
    try:
        data = read_request_json()
        task_ids = data.get("task_ids") or []
        if not isinstance(task_ids, list):
            return jsonify({"status": "error", "message": "task_ids 必须是数组"}), 400
        task_ids = [str(item).strip() for item in task_ids if str(item).strip()]
        if not task_ids:
            return jsonify({"status": "error", "message": "请先选择要删除的任务"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(task_ids))
        cursor.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", task_ids)
        conn.commit()
        conn.close()

        with task_manager.lock:
            for task_id in task_ids:
                task_manager.tasks.pop(task_id, None)
                event = task_manager.events.pop(task_id, None)
                if event:
                    event.set()

        for task_id in task_ids:
            task_dir = os.path.join(OUTPUT_FOLDER, task_id)
            if os.path.exists(task_dir):
                shutil.rmtree(task_dir, ignore_errors=True)
            task_file = task_file_path(task_id)
            if os.path.exists(task_file):
                os.remove(task_file)

        return jsonify({"status": "success", "message": "已删除所选任务", "task_ids": task_ids})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8034"))
    host = os.environ.get("HOST", "127.0.0.1")
    print("\n" + "=" * 60)
    print("豆包视频 Cookie 池 API 服务")
    print("=" * 60)
    print(f"运行目录: {BASE_DIR}")
    print(f"数据库: {DB_PATH}")
    print(f"Cookies: {COOKIES_DIR}")
    print(f"输出目录: {OUTPUT_FOLDER}")
    print(f"最大并发: {MAX_WORKERS}")
    print(f"服务地址: http://{host}:{port}")
    print("=" * 60 + "\n")
    app.run(host=host, port=port, debug=False, threaded=True)
