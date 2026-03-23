"""
Dream-to-Video SQLite 数据库模块

替代原有的文件持久化（JSONL + JSON），支持多用户并发安全。
使用 aiosqlite 提供异步接口，与 FastAPI 事件循环兼容。
"""

import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DATABASE_PATH, DATA_DIR

# 全局数据库连接（单例）
_db: Optional[aiosqlite.Connection] = None


async def init_db() -> aiosqlite.Connection:
    """初始化数据库：创建表和索引，返回连接。"""
    global _db
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    _db = await aiosqlite.connect(str(DATABASE_PATH))
    _db.row_factory = aiosqlite.Row

    # 启用 WAL 模式（允许并发读写）
    await _db.execute("PRAGMA journal_mode=WAL")

    await _db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE NOT NULL,
            prompt TEXT NOT NULL,
            original_text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            submit_order INTEGER DEFAULT 0,
            submitted_at TEXT,
            completed_at TEXT,
            video_path TEXT,
            effect_video_path TEXT,
            error_message TEXT,
            error_type TEXT,
            retry_count INTEGER DEFAULT 0,
            reference_image_path TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_task_id ON tasks(task_id);

        CREATE TABLE IF NOT EXISTS downloaded_urls (
            url TEXT PRIMARY KEY,
            task_id TEXT,
            downloaded_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS worker_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
    """)
    await _db.commit()

    # 自动迁移：为旧数据库添加 original_text 列
    try:
        async with _db.execute("PRAGMA table_info(tasks)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        if "original_text" not in columns:
            await _db.execute("ALTER TABLE tasks ADD COLUMN original_text TEXT")
            await _db.commit()
    except Exception:
        pass  # 列已存在或其他非致命错误

    return _db


async def get_db() -> aiosqlite.Connection:
    """获取数据库连接，如未初始化则自动初始化。"""
    global _db
    if _db is None:
        await init_db()
    return _db


async def close_db():
    """关闭数据库连接。"""
    global _db
    if _db:
        await _db.close()
        _db = None


# ====== 任务操作 ======

async def add_task(prompt: str, original_text: Optional[str] = None) -> str:
    """添加新任务，返回 task_id。"""
    db = await get_db()

    # 生成递增 task_id
    async with db.execute("SELECT COUNT(*) FROM tasks") as cursor:
        row = await cursor.fetchone()
        count = row[0] if row else 0

    task_id = f"task_{count:03d}"
    now = datetime.now().isoformat()

    await db.execute(
        """INSERT INTO tasks (task_id, prompt, original_text, status, submit_order, created_at, updated_at)
           VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
        (task_id, prompt, original_text, count, now, now),
    )
    await db.commit()
    return task_id


async def get_task(task_id: str) -> Optional[dict]:
    """查询单个任务。"""
    db = await get_db()
    async with db.execute(
        "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_task(task_id: str, **kwargs):
    """更新任务字段。"""
    if not kwargs:
        return

    db = await get_db()
    kwargs["updated_at"] = datetime.now().isoformat()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]

    await db.execute(
        f"UPDATE tasks SET {fields} WHERE task_id = ?",
        values,
    )
    await db.commit()


async def get_pending_tasks() -> list[dict]:
    """获取所有待处理的任务（status='pending'），按创建时间排序。"""
    db = await get_db()
    async with db.execute(
        "SELECT * FROM tasks WHERE status = 'pending' ORDER BY created_at ASC"
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_submitted_tasks() -> list[dict]:
    """获取所有已提交等待完成的任务。"""
    db = await get_db()
    async with db.execute(
        "SELECT * FROM tasks WHERE status = 'submitted' ORDER BY submit_order ASC"
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def list_tasks(
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[dict], int]:
    """分页列出任务，返回 (tasks, total_count)。"""
    db = await get_db()

    where = ""
    params = []
    if status:
        where = "WHERE status = ?"
        params.append(status)

    # 总数
    async with db.execute(
        f"SELECT COUNT(*) FROM tasks {where}", params
    ) as cursor:
        row = await cursor.fetchone()
        total = row[0] if row else 0

    # 分页查询
    offset = (page - 1) * per_page
    async with db.execute(
        f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ) as cursor:
        rows = await cursor.fetchall()
        tasks = [dict(r) for r in rows]

    return tasks, total


# ====== 已下载 URL ======

async def is_url_downloaded(url: str) -> bool:
    """检查 URL 是否已下载。"""
    db = await get_db()
    async with db.execute(
        "SELECT 1 FROM downloaded_urls WHERE url = ?", (url,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def mark_url_downloaded(url: str, task_id: str):
    """标记 URL 为已下载。"""
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO downloaded_urls (url, task_id) VALUES (?, ?)",
        (url, task_id),
    )
    await db.commit()


async def get_all_downloaded_urls() -> set[str]:
    """获取所有已下载的 URL。"""
    db = await get_db()
    async with db.execute("SELECT url FROM downloaded_urls") as cursor:
        rows = await cursor.fetchall()
        return {r[0] for r in rows}


# ====== Worker 状态 ======

async def get_worker_state(key: str) -> Optional[str]:
    """读取 Worker 状态值。"""
    db = await get_db()
    async with db.execute(
        "SELECT value FROM worker_state WHERE key = ?", (key,)
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_worker_state(key: str, value: str):
    """设置 Worker 状态值。"""
    db = await get_db()
    now = datetime.now().isoformat()
    await db.execute(
        """INSERT INTO worker_state (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?""",
        (key, value, now, value, now),
    )
    await db.commit()


# ====== 数据迁移（从旧文件导入）======

async def migrate_from_files():
    """
    从旧的 JSONL/JSON 文件导入数据到 SQLite。
    仅在数据库中无任务时执行一次。
    """
    from config import PROMPT_QUEUE_FILE, BATCH_STATE_FILE

    db = await get_db()

    # 检查是否已有数据
    async with db.execute("SELECT COUNT(*) FROM tasks") as cursor:
        row = await cursor.fetchone()
        if row[0] > 0:
            return  # 已有数据，跳过迁移

    migrated = 0

    # 从 batch_state.json 导入（包含最完整的任务信息）
    if BATCH_STATE_FILE.exists():
        try:
            data = json.loads(BATCH_STATE_FILE.read_text(encoding="utf-8"))
            for task_data in data.get("tasks", []):
                now = datetime.now().isoformat()
                await db.execute(
                    """INSERT OR IGNORE INTO tasks
                       (task_id, prompt, status, submit_order, submitted_at,
                        completed_at, video_path, effect_video_path, error_message,
                        retry_count, reference_image_path, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task_data.get("task_id"),
                        task_data.get("prompt", ""),
                        task_data.get("status", "pending"),
                        task_data.get("submit_order", 0),
                        task_data.get("submitted_at"),
                        task_data.get("completed_at"),
                        task_data.get("video_path"),
                        task_data.get("effect_video_path"),
                        task_data.get("error_message"),
                        task_data.get("retry_count", 0),
                        task_data.get("reference_image_path"),
                        now, now,
                    ),
                )
                migrated += 1

            # 导入已下载 URL
            for url in data.get("downloaded_video_urls", []):
                await db.execute(
                    "INSERT OR IGNORE INTO downloaded_urls (url) VALUES (?)",
                    (url,),
                )

            await db.commit()
        except Exception as e:
            print(f"  ⚠ batch_state.json 迁移失败: {e}")

    # 从 prompt_queue.jsonl 补充（可能有 batch_state 中未记录的任务）
    if PROMPT_QUEUE_FILE.exists() and migrated == 0:
        try:
            for line in PROMPT_QUEUE_FILE.read_text(encoding="utf-8").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                now = datetime.now().isoformat()
                await db.execute(
                    """INSERT OR IGNORE INTO tasks
                       (task_id, prompt, status, created_at, updated_at)
                       VALUES (?, ?, 'pending', ?, ?)""",
                    (entry["task_id"], entry["prompt"], now, now),
                )
                migrated += 1
            await db.commit()
        except Exception as e:
            print(f"  ⚠ prompt_queue.jsonl 迁移失败: {e}")

    if migrated > 0:
        print(f"  ✓ 已从旧文件迁移 {migrated} 条任务到数据库")
