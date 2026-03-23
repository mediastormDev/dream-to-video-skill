"""
Dream-to-Video 命令行入口

用法:
    python main.py login                     # 扫码登录（首次使用）
    python main.py generate "你的提示词"      # 生成视频（同步，等待完成）
    python main.py worker                    # 启动后台 Worker（长时间运行）
    python main.py add "你的提示词"           # 向队列添加任务（Worker 自动处理）
    python main.py status                    # 查看任务状态
    python main.py verify                    # 验证登录状态
    python main.py serve                     # 启动 API 服务（可选）
"""

import asyncio
import json
import sys
import io
from pathlib import Path
from datetime import datetime

# Windows 终端 UTF-8 支持 + 行缓冲（确保后台运行时日志实时输出）
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# 确保项目根目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent))

from models import ProgressInfo, GenerationStatus


async def print_progress(info: ProgressInfo):
    """命令行进度回调：在终端打印进度信息"""
    icon = {
        GenerationStatus.PENDING: "⏳",
        GenerationStatus.SUBMITTING: "📝",
        GenerationStatus.GENERATING: "🎬",
        GenerationStatus.DOWNLOADING: "📥",
        GenerationStatus.COMPLETED: "✅",
        GenerationStatus.FAILED: "❌",
    }.get(info.status, "❓")

    percent_str = f" [{info.progress_percent}%]" if info.progress_percent is not None else ""
    print(f"  {icon} {info.message}{percent_str}")


async def cmd_login():
    """扫码登录"""
    from auth.login import save_auth_state
    await save_auth_state()


async def cmd_verify():
    """验证登录状态"""
    from auth.login import verify_auth
    is_valid = await verify_auth()
    if is_valid:
        print("✅ 登录状态有效，可以正常使用。")
    else:
        print("❌ 登录已过期或不存在，请运行: python main.py login")


async def cmd_generate(prompt: str):
    """生成视频"""
    from browser.engine import JimengBrowser

    print()
    print("=" * 50)
    print("  Dream-to-Video 视频生成")
    print("=" * 50)
    print()
    print(f"提示词: {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
    print()

    try:
        async with JimengBrowser() as browser:
            result = await browser.generate_video(
                prompt,
                progress_callback=print_progress,
            )

        print()
        print("=" * 50)
        print("  生成结果")
        print("=" * 50)

        # 输出结构化结果
        result_dict = result.model_dump(exclude_none=True)
        # 将 datetime 转换为字符串
        for key, val in result_dict.items():
            if isinstance(val, datetime):
                result_dict[key] = val.isoformat()

        print(json.dumps(result_dict, ensure_ascii=False, indent=2))

        if result.status == GenerationStatus.COMPLETED:
            print()
            print(f"✅ 视频已保存到: {result.video_path}")
        elif result.status == GenerationStatus.FAILED:
            print()
            print(f"❌ 生成失败: {result.error_message}")

    except RuntimeError as e:
        print(f"\n❌ 错误: {e}")
    except Exception as e:
        print(f"\n❌ 未知错误: {e}")


def cmd_serve(port: int = 8080):
    """启动 FastAPI 服务"""
    try:
        import uvicorn
        from api.server import app
        print(f"正在启动 API 服务器 (端口: {port})...")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except ImportError:
        print("错误: 需要安装 FastAPI 和 uvicorn")
        print("运行: pip install fastapi uvicorn")


def cmd_add(prompt: str):
    """向队列添加一个 prompt（瞬间完成，Worker 自动处理）"""
    from batch.persistence import add_to_queue

    task_id = add_to_queue(prompt)
    prompt_preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
    print(f"✓ [{task_id}] 已加入队列: {prompt_preview}")
    print("  Worker 将自动提交并监控生成进度。")


async def cmd_worker():
    """启动后台 Worker"""
    from batch.worker import run_worker
    await run_worker()


def cmd_status():
    """查看当前任务状态"""
    from batch.persistence import load_batch_state, read_queue

    state = load_batch_state()
    queue = read_queue()

    print()
    print("=" * 50)
    print("  任务状态")
    print("=" * 50)

    if not state and not queue:
        print("  暂无任务。")
        print('  使用 python main.py add "提示词" 添加任务')
        print()
        return

    # 队列中的待处理条目
    if state:
        known_ids = {t.task_id for t in state.tasks}
        pending_in_queue = [e for e in queue if e["task_id"] not in known_ids]
    else:
        pending_in_queue = queue

    if pending_in_queue:
        print(f"\n  📥 队列中等待处理: {len(pending_in_queue)} 个")
        for entry in pending_in_queue:
            prompt_preview = entry["prompt"][:40] + ("..." if len(entry["prompt"]) > 40 else "")
            print(f"    [{entry['task_id']}] {prompt_preview}")

    # 已处理的任务
    if state and state.tasks:
        completed = [t for t in state.tasks if t.status.value == "completed"]
        submitted = [t for t in state.tasks if t.status.value == "submitted"]
        failed = [t for t in state.tasks if t.status.value == "failed"]

        if submitted:
            print(f"\n  🎬 等待生成完成: {len(submitted)} 个")
            for t in submitted:
                prompt_preview = t.prompt[:40] + ("..." if len(t.prompt) > 40 else "")
                print(f"    [{t.task_id}] {prompt_preview}")

        if completed:
            print(f"\n  ✅ 已完成: {len(completed)} 个")
            for t in completed:
                print(f"    [{t.task_id}] 原版 → {t.video_path or '未知路径'}")
                if t.effect_video_path:
                    print(f"    [{t.task_id}] 特效 → {t.effect_video_path}")

        if failed:
            print(f"\n  ❌ 失败: {len(failed)} 个")
            for t in failed:
                print(f"    [{t.task_id}] {t.error_message or '未知错误'}")

    print()


def show_help():
    """显示帮助信息"""
    print()
    print("Dream-to-Video - 即梦视频自动生成工具")
    print()
    print("用法:")
    print("  python main.py login                    扫码登录（首次使用必须执行）")
    print("  python main.py verify                   检查登录是否过期")
    print()
    print("  --- Worker 模式（推荐）---")
    print("  python main.py worker                   启动后台 Worker（长时间运行）")
    print('  python main.py add "你的提示词"          向队列添加任务')
    print("  python main.py status                   查看所有任务状态")
    print()
    print("  --- 单次模式 ---")
    print('  python main.py generate "你的提示词"     单次生成视频（同步等待）')
    print()
    print("  --- 其他 ---")
    print("  python main.py serve                    启动 API 服务器（可选）")
    print("  python main.py help                     显示此帮助信息")
    print()
    print("推荐流程:")
    print("  1. 先运行 worker（后台）")
    print('  2. 然后用 add 添加任务，Worker 自动提交+监控+下载')
    print()


def main():
    if len(sys.argv) < 2:
        show_help()
        return

    command = sys.argv[1].lower()

    if command == "login":
        asyncio.run(cmd_login())

    elif command == "verify":
        asyncio.run(cmd_verify())

    elif command == "generate":
        if len(sys.argv) < 3:
            print("错误: 请提供提示词")
            print('用法: python main.py generate "你的提示词"')
            return
        prompt = sys.argv[2]
        asyncio.run(cmd_generate(prompt))

    elif command == "worker":
        asyncio.run(cmd_worker())

    elif command == "add":
        if len(sys.argv) < 3:
            print("错误: 请提供提示词")
            print('用法: python main.py add "你的提示词"')
            return
        prompt = sys.argv[2]
        cmd_add(prompt)

    elif command == "status":
        cmd_status()

    elif command == "serve":
        port = 8080
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                port = int(sys.argv[idx + 1])
        cmd_serve(port)

    elif command in ("help", "-h", "--help"):
        show_help()

    else:
        print(f"未知命令: {command}")
        show_help()


if __name__ == "__main__":
    main()
