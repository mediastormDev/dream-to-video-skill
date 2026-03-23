"""
Dream-to-Video FastAPI 服务端

提供：
- 公开 API：提交梦境文字、查询任务状态、下载视频、SSE 进度推送
- 管理接口：QR 码远程登录、Worker 状态监控
- Web 前端：静态文件服务
"""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    OUTPUT_DIR, ADMIN_TOKEN, VIDEO_RETENTION_DAYS,
    HEADLESS, JIMENG_VIDEO_URL, PAGE_LOAD_TIMEOUT,
    ANTHROPIC_API_KEY,
)
from database import (
    init_db, close_db, migrate_from_files,
    add_task, get_task, update_task, list_tasks,
    get_pending_tasks, get_submitted_tasks,
)
from prompt_engine import transform_dream_to_prompt

logger = logging.getLogger(__name__)

# ====== Cookie 存储路径 ======
COOKIES_FILE = Path(__file__).parent.parent / "data" / "jimeng_cookies.json"

# ====== Worker 全局状态 ======
worker_task: Optional[asyncio.Task] = None
worker_running = False
browser_instance = None  # JimengBrowser 实例，供 QR 码截图使用
qr_screenshot_data: Optional[bytes] = None
login_in_progress = False


# ====== 生命周期 ======

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动和关闭时的逻辑。"""
    # 启动
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    await init_db()
    await migrate_from_files()

    # 启动后台 Worker
    global worker_task, worker_running
    worker_running = True
    worker_task = asyncio.create_task(_worker_loop())
    logger.info("Worker 后台任务已启动")

    # 启动定时清理任务
    cleanup_task = asyncio.create_task(_cleanup_loop())

    yield

    # 关闭
    worker_running = False
    if worker_task:
        worker_task.cancel()
    cleanup_task.cancel()

    global browser_instance
    if browser_instance:
        await browser_instance.__aexit__(None, None, None)
        browser_instance = None

    await close_db()
    logger.info("服务已关闭")


app = FastAPI(
    title="Dream-to-Video API",
    version="1.0.0",
    lifespan=lifespan,
)

# 静态文件服务
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ====== 请求/响应模型 ======

class GenerateRequest(BaseModel):
    prompt: Optional[str] = None         # 已转化的提示词（直接提交）
    dream_text: Optional[str] = None     # 原始梦境文字（需要 AI 转化）
    api_key: Optional[str] = None        # 用户提供的 API Key
    provider: Optional[str] = "claude"   # API 提供商: claude/openai/openrouter/gemini


class TaskResponse(BaseModel):
    task_id: str
    prompt: str
    status: str
    video_url: Optional[str] = None
    effect_video_url: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


# ====== Admin 认证 ======

security = HTTPBearer(auto_error=False)


async def verify_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    if not ADMIN_TOKEN:
        raise HTTPException(500, "ADMIN_TOKEN 未配置，请设置环境变量")
    if not credentials or credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(401, "未授权：请提供正确的 Admin Token")
    return True


# ====== 公开 API ======

@app.post("/api/generate")
async def api_generate(req: GenerateRequest):
    """提交梦境文字，创建视频生成任务。

    支持两种模式：
    - dream_text: 提供原始梦境文字，服务端调用 Claude API 自动转化为视频提示词
    - prompt: 直接提供已转化的提示词（跳过 AI 转化）
    """
    original_text = None
    prompt = None

    if req.dream_text:
        # 模式 1：原始梦境文字 → AI 转化
        original_text = req.dream_text.strip()
        if not original_text:
            raise HTTPException(400, "梦境文字不能为空")
        if len(original_text) > 5000:
            raise HTTPException(400, "梦境文字过长（最大 5000 字）")
        # 使用用户提供的 API Key
        user_key = req.api_key
        if not user_key:
            raise HTTPException(400, "请提供 API Key")

        provider = req.provider or "claude"
        try:
            prompt = await transform_dream_to_prompt(original_text, api_key=user_key, provider=provider)
        except Exception as e:
            logger.error(f"Prompt 转化失败: {e}")
            raise HTTPException(500, f"AI 提示词转化失败: {str(e)}")

    elif req.prompt:
        # 模式 2：直接提供提示词
        prompt = req.prompt.strip()
        if not prompt:
            raise HTTPException(400, "提示词不能为空")
        if len(prompt) > 5000:
            raise HTTPException(400, "提示词过长（最大 5000 字）")
    else:
        raise HTTPException(400, "请提供 dream_text（梦境文字）或 prompt（提示词）")

    task_id = await add_task(prompt, original_text=original_text)
    return {
        "task_id": task_id,
        "status": "pending",
        "prompt": prompt,
        "message": "任务已加入队列，AI 已将梦境文字转化为视频提示词" if original_text else "任务已加入队列",
    }


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    """查询任务状态。"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    result = {
        "task_id": task["task_id"],
        "prompt": task["prompt"],
        "original_text": task.get("original_text"),
        "status": task["status"],
        "error_message": task.get("error_message"),
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
    }

    # 添加视频下载链接
    if task.get("video_path") and Path(task["video_path"]).exists():
        result["video_url"] = f"/api/tasks/{task_id}/video"
    if task.get("effect_video_path") and Path(task["effect_video_path"]).exists():
        result["effect_video_url"] = f"/api/tasks/{task_id}/video?effect=true"

    return result


@app.get("/api/tasks/{task_id}/video")
async def api_get_video(task_id: str, effect: bool = False):
    """下载视频文件。"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    path_key = "effect_video_path" if effect else "video_path"
    video_path = task.get(path_key)

    if not video_path or not Path(video_path).exists():
        raise HTTPException(404, "视频文件不存在")

    suffix = "_effect" if effect else ""
    return FileResponse(
        video_path,
        media_type="video/mp4",
        filename=f"{task_id}{suffix}.mp4",
    )


@app.get("/api/tasks")
async def api_list_tasks(
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """分页列出任务。"""
    tasks, total = await list_tasks(status=status, page=page, per_page=per_page)

    items = []
    for t in tasks:
        item = {
            "task_id": t["task_id"],
            "prompt": t["prompt"][:100] + ("..." if len(t["prompt"]) > 100 else ""),
            "status": t["status"],
            "created_at": t.get("created_at"),
            "completed_at": t.get("completed_at"),
            "error_message": t.get("error_message"),
        }
        if t.get("video_path") and Path(t["video_path"]).exists():
            item["video_url"] = f"/api/tasks/{t['task_id']}/video"
        if t.get("effect_video_path") and Path(t["effect_video_path"]).exists():
            item["effect_video_url"] = f"/api/tasks/{t['task_id']}/video?effect=true"
        items.append(item)

    return {
        "tasks": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@app.get("/api/tasks/{task_id}/progress")
async def api_task_progress(task_id: str, request: Request):
    """SSE 实时进度推送。"""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break

            current = await get_task(task_id)
            if not current:
                yield {"data": '{"status": "not_found"}'}
                break

            data = {
                "status": current["status"],
                "error_message": current.get("error_message"),
            }

            if current.get("video_path") and Path(current["video_path"]).exists():
                data["video_url"] = f"/api/tasks/{task_id}/video"
            if current.get("effect_video_path") and Path(current["effect_video_path"]).exists():
                data["effect_video_url"] = f"/api/tasks/{task_id}/video?effect=true"

            import json
            yield {"data": json.dumps(data, ensure_ascii=False)}

            # 终态停止推送
            if current["status"] in ("completed", "failed"):
                break

            await asyncio.sleep(3)

    return EventSourceResponse(event_generator())


# ====== 管理接口 ======

@app.get("/admin/status")
async def admin_status(authorized: bool = Depends(verify_admin)):
    """Worker 和系统状态。"""
    pending = await get_pending_tasks()
    submitted = await get_submitted_tasks()
    _, total = await list_tasks(page=1, per_page=1)

    return {
        "worker_running": worker_running and worker_task and not worker_task.done(),
        "browser_active": browser_instance is not None,
        "login_in_progress": login_in_progress,
        "queue_pending": len(pending),
        "queue_submitted": len(submitted),
        "total_tasks": total,
        "headless_mode": HEADLESS,
    }


@app.post("/admin/login")
async def admin_trigger_login(authorized: bool = Depends(verify_admin)):
    """触发即梦 QR 码登录流程。"""
    global login_in_progress
    if login_in_progress:
        return {"message": "登录流程已在进行中，请查看 /admin/qr-code"}

    login_in_progress = True
    asyncio.create_task(_perform_login_flow())
    return {"message": "登录流程已启动，请访问 /admin/qr-code 获取二维码"}


@app.get("/admin/qr-code")
async def admin_get_qr_code(authorized: bool = Depends(verify_admin)):
    """获取当前 QR 码截图。"""
    if qr_screenshot_data is None:
        raise HTTPException(
            404,
            "暂无二维码截图。请先 POST /admin/login 触发登录流程。",
        )
    return Response(content=qr_screenshot_data, media_type="image/png")


@app.post("/admin/upload-cookies")
async def admin_upload_cookies(request: Request, authorized: bool = Depends(verify_admin)):
    """
    上传即梦登录 Cookie。

    接收从本地 export_cookies.py 脚本或 admin 页面发来的 Cookie 列表，
    保存到服务器，浏览器启动时自动加载。
    """
    try:
        cookies = await request.json()
        if not isinstance(cookies, list):
            raise HTTPException(400, "Cookie 数据格式错误，应为列表")

        # 确保目录存在
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)

        # 保存 Cookie
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

        logger.info(f"已保存 {len(cookies)} 个 Cookie 到 {COOKIES_FILE}")

        # 如果浏览器已经启动，立即注入 Cookie
        if browser_instance and browser_instance._context:
            try:
                await _inject_cookies(browser_instance._context)
                logger.info("Cookie 已注入到当前浏览器")
            except Exception as e:
                logger.warning(f"Cookie 注入失败（将在下次启动时生效）: {e}")

        return {
            "message": f"Cookie 上传成功！已保存 {len(cookies)} 个 Cookie。",
            "count": len(cookies),
        }

    except json.JSONDecodeError:
        raise HTTPException(400, "无效的 JSON 数据")


@app.get("/admin/cookie-status")
async def admin_cookie_status(authorized: bool = Depends(verify_admin)):
    """检查 Cookie 状态。"""
    if not COOKIES_FILE.exists():
        return {"has_cookies": False, "count": 0, "message": "未上传 Cookie"}

    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        return {
            "has_cookies": True,
            "count": len(cookies),
            "message": f"已有 {len(cookies)} 个 Cookie",
        }
    except Exception:
        return {"has_cookies": False, "count": 0, "message": "Cookie 文件损坏"}


async def _inject_cookies(context):
    """将保存的 Cookie 注入到浏览器上下文。"""
    if not COOKIES_FILE.exists():
        return False

    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        if not cookies:
            return False

        # Playwright 的 add_cookies 需要特定格式
        valid_cookies = []
        for c in cookies:
            cookie = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            # 可选字段
            if c.get("expires"):
                cookie["expires"] = c["expires"]
            if c.get("httpOnly") is not None:
                cookie["httpOnly"] = c["httpOnly"]
            if c.get("secure") is not None:
                cookie["secure"] = c["secure"]
            if c.get("sameSite"):
                cookie["sameSite"] = c["sameSite"]

            if cookie["name"] and cookie["domain"]:
                valid_cookies.append(cookie)

        if valid_cookies:
            await context.add_cookies(valid_cookies)
            logger.info(f"已注入 {len(valid_cookies)} 个 Cookie")
            return True

    except Exception as e:
        logger.error(f"Cookie 注入失败: {e}")

    return False


# ====== 前端页面 ======

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """用户主页。"""
    index_file = static_dir / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Dream-to-Video</h1><p>前端文件未找到，请检查 static/ 目录。</p>")
    return FileResponse(str(index_file))


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    """管理后台页面。"""
    admin_file = static_dir / "admin.html"
    if not admin_file.exists():
        return HTMLResponse("<h1>Admin</h1><p>管理页面文件未找到。</p>")
    return FileResponse(str(admin_file))


# ====== Worker 后台循环 ======

async def _get_or_create_browser():
    """获取或创建浏览器实例。"""
    global browser_instance
    if browser_instance is None:
        from browser.engine import JimengBrowser
        browser_instance = JimengBrowser(headless=HEADLESS)
        await browser_instance.__aenter__()
        logger.info("浏览器实例已创建")
    return browser_instance


async def _worker_loop():
    """Worker 主循环：从数据库读取待处理任务，逐个提交到即梦。"""
    from batch.worker import VideoWorker
    from batch.persistence import load_batch_state, save_batch_state, get_new_prompts
    from models import BatchState, BatchTask, GenerationStatus
    from config import WORKER_POLL_INTERVAL

    logger.info("Worker 循环启动")

    # Worker 仍使用原有的 VideoWorker 类（它自己管理浏览器和状态）
    # 同时轮询数据库中的新任务，同步到文件队列
    worker = None

    while worker_running:
        try:
            # 检查数据库中是否有新的 pending 任务
            pending = await get_pending_tasks()

            if pending:
                for task_data in pending:
                    # 标记为 submitting，表示 Worker 已接管
                    await update_task(task_data["task_id"], status="submitting")

                # 延迟初始化 Worker（首次有任务时才启动浏览器）
                if worker is None:
                    worker = VideoWorker()
                    global browser_instance
                    from browser.engine import JimengBrowser
                    browser_instance = JimengBrowser(headless=HEADLESS)
                    await browser_instance.__aenter__()
                    worker.browser = browser_instance
                    worker.state.settings_configured = False
                    logger.info("Worker 浏览器已就绪")

                # 逐个处理任务
                for task_data in pending:
                    task_id = task_data["task_id"]
                    prompt = task_data["prompt"]

                    try:
                        logger.info(f"[{task_id}] 开始处理: {prompt[:50]}...")
                        await update_task(task_id, status="submitted",
                                          submitted_at=datetime.now().isoformat())

                        # 使用 Worker 的提交逻辑
                        prompt_data = {"task_id": task_id, "prompt": prompt}
                        await worker._submit_one(prompt_data)

                        logger.info(f"[{task_id}] 已提交到即梦")

                    except Exception as e:
                        logger.error(f"[{task_id}] 提交失败: {e}")
                        await update_task(
                            task_id,
                            status="failed",
                            error_message=str(e),
                            completed_at=datetime.now().isoformat(),
                        )

                # 提交完成后，检查已提交任务的完成情况
                if worker and worker.browser:
                    await _check_completions(worker)

            else:
                # 没有新任务，但要检查已提交任务的完成情况
                submitted = await get_submitted_tasks()
                if submitted and worker and worker.browser:
                    await _check_completions(worker)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker 循环异常: {e}")

        await asyncio.sleep(WORKER_POLL_INTERVAL)

    # 清理
    if browser_instance:
        try:
            await browser_instance.__aexit__(None, None, None)
        except Exception:
            pass
        browser_instance = None

    logger.info("Worker 循环已退出")


async def _check_completions(worker):
    """检查页面上已完成的视频，更新数据库。"""
    from browser.selectors import JimengSelectors
    from browser.reference_image import needs_reference_image
    from config import REFERENCE_IMAGE_PREFIX, OUTPUT_DIR

    sel = JimengSelectors
    submitted = await get_submitted_tasks()
    if not submitted:
        return

    cards = worker.browser.page.locator(sel.GENERATION_RESULT_CARD)
    try:
        card_count = await cards.count()
    except Exception:
        return

    for i in range(card_count):
        try:
            card = cards.nth(i)
            card_text = (await card.text_content() or "").strip()
            if not card_text:
                continue

            # 审核未通过检测
            moderation_keywords = ["审核未通过", "审核失败", "未通过审核", "内容违规"]
            is_moderation_failed = any(kw in card_text for kw in moderation_keywords)

            # 匹配任务
            matched = None
            for task in submitted:
                snippet = task["prompt"][:20]
                if snippet in card_text:
                    matched = task
                    break
                if task["prompt"].startswith(REFERENCE_IMAGE_PREFIX):
                    body = task["prompt"][len(REFERENCE_IMAGE_PREFIX):][:20]
                    if body in card_text:
                        matched = task
                        break

            if not matched:
                continue

            if is_moderation_failed:
                await update_task(
                    matched["task_id"],
                    status="failed",
                    error_message="审核未通过",
                    completed_at=datetime.now().isoformat(),
                )
                logger.warning(f"[{matched['task_id']}] 审核未通过")
                submitted.remove(matched)
                continue

            # 检查视频
            video_el = card.locator("video")
            if await video_el.count() == 0:
                continue

            url = await video_el.first.get_attribute("src")
            if not url:
                source = video_el.first.locator("source")
                if await source.count() > 0:
                    url = await source.first.get_attribute("src")

            if not url or not url.startswith("http"):
                continue

            # 检查是否已下载
            from database import is_url_downloaded, mark_url_downloaded
            if await is_url_downloaded(url):
                continue

            # 下载视频
            task_id = matched["task_id"]
            logger.info(f"[{task_id}] 视频已生成，下载中...")
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = OUTPUT_DIR / f"{task_id}_{timestamp}.mp4"

            try:
                response = await worker.browser.page.request.get(url)
                if response.ok:
                    body = await response.body()
                    output_file.write_bytes(body)

                    if output_file.exists() and output_file.stat().st_size > 0:
                        await mark_url_downloaded(url, task_id)

                        # 后处理特效
                        effect_path = await worker._post_process_video(output_file, task_id)

                        await update_task(
                            task_id,
                            status="completed",
                            video_path=str(output_file),
                            effect_video_path=str(effect_path) if effect_path else None,
                            completed_at=datetime.now().isoformat(),
                        )
                        logger.info(f"[{task_id}] 完成: {output_file}")
                        submitted.remove(matched)
            except Exception as e:
                logger.error(f"[{task_id}] 下载失败: {e}")

        except Exception:
            continue


# ====== QR 码登录流程 ======

async def _perform_login_flow():
    """在后台执行登录流程（优先尝试 Cookie 注入，失败后回退到 QR 码）。"""
    global qr_screenshot_data, login_in_progress, browser_instance

    try:
        from config import JIMENG_VIDEO_URL, LOGIN_TIMEOUT
        from browser.stealth import apply_stealth
        from playwright.async_api import async_playwright

        logger.info("开始登录流程")

        # 启动独立浏览器进行登录
        pw_cm = async_playwright()
        pw = await pw_cm.__aenter__()

        from config import USER_DATA_DIR, SLOW_MO, VIEWPORT
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=HEADLESS,
            slow_mo=SLOW_MO,
            viewport=VIEWPORT,
            locale="zh-CN",
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await apply_stealth(page)

        # 优先尝试从保存的 Cookie 登录
        cookie_injected = await _inject_cookies(context)
        if cookie_injected:
            logger.info("已注入保存的 Cookie，验证登录状态...")

        # 导航到即梦视频生成页
        await page.goto(JIMENG_VIDEO_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        # 如果注入了 Cookie，检查是否已成功登录
        if cookie_injected:
            login_prompts = page.locator(
                '[class*="login-modal"], [class*="qr-code"], '
                '[class*="login-dialog"], [class*="sign-in"]'
            )
            is_login_needed = await login_prompts.count() > 0

            # 也检查是否有输入框（已登录的标志）
            from browser.selectors import JimengSelectors
            input_el = page.locator(JimengSelectors.PROMPT_INPUT)
            has_input = await input_el.count() > 0

            if has_input and not is_login_needed:
                logger.info("Cookie 登录成功！页面已就绪。")
                await context.close()
                await pw_cm.__aexit__(None, None, None)
                qr_screenshot_data = None
                login_in_progress = False

                # 重启 Worker 浏览器以使用新 Cookie
                if browser_instance:
                    try:
                        await browser_instance.__aexit__(None, None, None)
                    except Exception:
                        pass
                    browser_instance = None

                return
            else:
                logger.info("Cookie 登录未生效，继续尝试其他方式...")

        # 用于跟踪当前应该截图的页面（可能是弹窗）
        qr_page = page

        # 监听弹出窗口（即梦登录会弹出新窗口到 open.douyin.com）
        popup_page = None

        def on_popup(new_page):
            nonlocal popup_page, qr_page
            popup_page = new_page
            qr_page = new_page
            logger.info(f"检测到弹出窗口: {new_page.url}")

        context.on("page", on_popup)

        # 尝试点击页面上的"登录"按钮来触发登录弹窗
        login_clicked = False
        for selector in ['text="登录"', 'text="登录/注册"', 'text="立即登录"', 'text="开启即梦"', 'button:has-text("登录")']:
            try:
                btn = page.locator(selector)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    logger.info(f"已点击按钮: {selector}")
                    login_clicked = True
                    # 等待弹窗出现（最多等 10 秒）
                    for _ in range(20):
                        await page.wait_for_timeout(500)
                        if popup_page:
                            break
                    break
            except Exception:
                continue

        if not login_clicked:
            logger.info("未找到登录按钮，页面可能已登录")

        # 如果弹出了新窗口，等待其真正加载完成（不是 about:blank）
        if popup_page:
            try:
                # 等待弹窗 URL 从 about:blank 变为真实地址（最多 15 秒）
                for _ in range(30):
                    if popup_page.url and popup_page.url != "about:blank":
                        break
                    await asyncio.sleep(0.5)
                logger.info(f"弹窗 URL: {popup_page.url}")

                # 等待页面完全加载（networkidle 确保所有资源加载完毕）
                await popup_page.wait_for_load_state("networkidle", timeout=20000)
                logger.info("弹窗 networkidle 已就绪")

                # 额外等待 QR 码图片渲染
                try:
                    await popup_page.locator('img, canvas, svg').first.wait_for(
                        state="visible", timeout=10000
                    )
                    logger.info("弹窗中检测到可见图片/canvas 元素")
                except Exception:
                    logger.info("未检测到特定图片元素，继续...")

                # 额外等待确保渲染完成
                await popup_page.wait_for_timeout(2000)
                logger.info(f"弹窗页面已完全加载: {popup_page.url}")

            except Exception as e:
                logger.warning(f"弹窗加载异常: {e}")
                # 即使异常也额外等几秒再截图
                await asyncio.sleep(3)

        # 检查主页面是否有登录弹窗（模态框，非弹窗模式）
        if not popup_page:
            qr_selectors = ['img[src*="qrcode"]', 'img[src*="qr"]', '[class*="qr-code"]', '[class*="qrcode"]', 'canvas']
            for sel in qr_selectors:
                try:
                    el = page.locator(sel)
                    if await el.count() > 0 and await el.first.is_visible():
                        logger.info(f"在主页面检测到 QR 码元素: {sel}")
                        break
                except Exception:
                    continue

        # 截取当前页面截图（弹窗页面优先，否则主页面）
        try:
            if popup_page and not popup_page.is_closed():
                qr_screenshot_data = await popup_page.screenshot(type="png")
                logger.info(f"截图已生成 (来源: 弹窗, URL: {popup_page.url})")
            else:
                qr_screenshot_data = await page.screenshot(type="png")
                logger.info("截图已生成 (来源: 主页面)")
        except Exception as e:
            logger.warning(f"首次截图异常: {e}")
            qr_screenshot_data = await page.screenshot(type="png")

        # 轮询等待登录成功
        poll_interval = 5
        max_attempts = LOGIN_TIMEOUT // poll_interval
        popup_was_open = popup_page is not None and not popup_page.is_closed()

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            # 更新截图：优先截弹窗，弹窗关闭后截主页面
            try:
                if popup_page and not popup_page.is_closed():
                    qr_screenshot_data = await popup_page.screenshot(type="png")
                    popup_was_open = True
                else:
                    if popup_was_open and popup_page and popup_page.is_closed():
                        logger.info("弹窗已关闭，切换到主页面截图")
                    qr_screenshot_data = await page.screenshot(type="png")
            except Exception as e:
                logger.debug(f"截图异常: {e}")
                try:
                    qr_screenshot_data = await page.screenshot(type="png")
                except Exception:
                    pass

            # 检测登录成功
            logged_in = False

            # 方式 1：弹窗关闭（用户扫码后弹窗自动关闭 = 登录成功）
            if popup_was_open and popup_page and popup_page.is_closed():
                logger.info("弹窗已关闭，判定登录成功")
                await page.wait_for_timeout(3000)
                try:
                    await page.reload()
                    await page.wait_for_timeout(5000)
                except Exception:
                    pass
                logged_in = True

            # 方式 2：检查主页面是否有真实用户头像（CDN 链接）
            if not logged_in:
                try:
                    avatar_imgs = page.locator('img[src*="avatar"][src*="http"]')
                    if await avatar_imgs.count() > 0:
                        for i in range(await avatar_imgs.count()):
                            src = await avatar_imgs.nth(i).get_attribute("src") or ""
                            if "http" in src and ("cdn" in src or "tos" in src or "byte" in src):
                                logged_in = True
                                logger.info(f"检测到用户头像: {src[:80]}...")
                                break
                except Exception:
                    pass

            if logged_in:
                logger.info("QR 码登录成功！")
                try:
                    await page.goto(
                        JIMENG_VIDEO_URL,
                        wait_until="domcontentloaded",
                        timeout=PAGE_LOAD_TIMEOUT * 1000,
                    )
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass
                await context.close()
                await pw_cm.__aexit__(None, None, None)
                qr_screenshot_data = None
                login_in_progress = False

                if browser_instance:
                    try:
                        await browser_instance.__aexit__(None, None, None)
                    except Exception:
                        pass
                    browser_instance = None

                return

            if attempt % 6 == 0:
                elapsed = (attempt + 1) * poll_interval
                logger.info(f"等待扫码登录... ({elapsed}s / {LOGIN_TIMEOUT}s)")
                if popup_page:
                    logger.info(f"  弹窗状态: {'已关闭' if popup_page.is_closed() else '打开中'}, URL: {popup_page.url if not popup_page.is_closed() else 'N/A'}")

        logger.warning("QR 码登录超时")
        await context.close()
        await pw_cm.__aexit__(None, None, None)

    except Exception as e:
        logger.error(f"QR 码登录异常: {e}")
    finally:
        login_in_progress = False


# ====== 视频清理 ======

async def _cleanup_loop():
    """定时清理过期视频文件。"""
    while True:
        try:
            await asyncio.sleep(3600)  # 每小时检查一次

            if VIDEO_RETENTION_DAYS <= 0:
                continue

            cutoff = datetime.now() - timedelta(days=VIDEO_RETENTION_DAYS)
            cleaned = 0

            for f in OUTPUT_DIR.iterdir():
                if f.suffix == ".mp4" and f.stat().st_mtime < cutoff.timestamp():
                    f.unlink(missing_ok=True)
                    cleaned += 1

            if cleaned > 0:
                logger.info(f"清理了 {cleaned} 个过期视频文件")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"视频清理异常: {e}")
