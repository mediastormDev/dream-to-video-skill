"""
批量任务持久化：队列文件读写 + 状态保存/恢复。

支持两种模式：
- 同步模式（CLI 用）：保留原有文件操作，兼容 python main.py add/status 命令
- 异步模式（API/Worker 用）：通过 database.py 操作 SQLite
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import PROMPT_QUEUE_FILE, BATCH_STATE_FILE, OUTPUT_DIR, PROCESSED_IDS_FILE
from models import BatchState, BatchTask, GenerationStatus


# ====== 已处理 ID 追踪（轻量级，防重复提交）======

def _load_processed_ids() -> set:
    """读取已处理的 task_id 集合。即使 batch_state 损坏，这个文件也能防止重复提交。"""
    if not PROCESSED_IDS_FILE.exists():
        return set()
    try:
        text = PROCESSED_IDS_FILE.read_text(encoding="utf-8").strip()
        return {line.strip() for line in text.splitlines() if line.strip()}
    except Exception:
        return set()


def mark_task_processed(task_id: str):
    """将 task_id 追加到已处理列表（append-only，不会丢失）。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROCESSED_IDS_FILE, "a", encoding="utf-8") as f:
        f.write(task_id + "\n")


# ====== 队列文件操作（prompt_queue.jsonl）— CLI 同步模式 ======

def add_to_queue(prompt: str) -> str:
    """
    向队列文件追加一条 prompt（同步版，CLI 用）。
    同时写入 SQLite（如果数据库可用）。
    返回分配的 task_id。
    """
    import asyncio

    # 优先尝试通过数据库添加
    try:
        from database import add_task, init_db
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(init_db())
            task_id = loop.run_until_complete(add_task(prompt))
        finally:
            loop.close()
        return task_id
    except Exception:
        pass

    # 回退到文件模式
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    existing_count = 0
    if PROMPT_QUEUE_FILE.exists():
        existing_count = sum(1 for line in PROMPT_QUEUE_FILE.read_text(encoding="utf-8").strip().splitlines() if line.strip())

    state = load_batch_state()
    if state:
        existing_count = max(existing_count, len(state.tasks))

    task_id = f"task_{existing_count:03d}"

    entry = {
        "task_id": task_id,
        "prompt": prompt,
        "added_at": datetime.now().isoformat(),
    }

    with open(PROMPT_QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return task_id


def read_queue() -> list[dict]:
    """
    读取队列文件中的所有条目。
    返回 [{"task_id": ..., "prompt": ..., "added_at": ...}, ...]
    """
    if not PROMPT_QUEUE_FILE.exists():
        return []

    entries = []
    for line in PROMPT_QUEUE_FILE.read_text(encoding="utf-8").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def get_new_prompts(state: BatchState) -> list[dict]:
    """
    返回队列中尚未被 Worker 处理的新 prompt。

    双重检查机制：
    1. batch_state.tasks 中已有的 task_id（主状态）
    2. processed_ids.txt 中已记录的 task_id（备份，防止状态文件损坏后重复提交）
    """
    queue_entries = read_queue()

    known_ids = {t.task_id for t in state.tasks}
    processed_ids = _load_processed_ids()
    all_known = known_ids | processed_ids

    return [e for e in queue_entries if e["task_id"] not in all_known]


# ====== 状态文件操作（batch_state.json）======

def save_batch_state(state: BatchState):
    """保存批次状态到 JSON 文件。每次状态变更后调用。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    data = state.model_dump(mode="json")
    BATCH_STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def load_batch_state() -> Optional[BatchState]:
    """从 JSON 文件加载批次状态。文件不存在则返回 None。"""
    if not BATCH_STATE_FILE.exists():
        return None

    try:
        data = json.loads(BATCH_STATE_FILE.read_text(encoding="utf-8"))
        return BatchState.model_validate(data)
    except Exception as e:
        print(f"  ⚠ 无法加载批次状态: {e}")
        return None


def clear_state():
    """清空状态文件和队列文件（重新开始）。"""
    if BATCH_STATE_FILE.exists():
        BATCH_STATE_FILE.unlink()
    if PROMPT_QUEUE_FILE.exists():
        PROMPT_QUEUE_FILE.unlink()
    if PROCESSED_IDS_FILE.exists():
        PROCESSED_IDS_FILE.unlink()
