"""进度追踪器 - 通过轮询 DOM 状态监控视频生成进度"""

import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Optional, Callable, Awaitable

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import Page
from models import GenerationStatus, ProgressInfo, ErrorType
from config import GENERATION_TIMEOUT, POLL_INTERVAL, HEARTBEAT_INTERVAL

ProgressCallback = Callable[[ProgressInfo], Awaitable[None]]


class ProgressTracker:
    """
    多策略 DOM 轮询进度追踪器。

    关键改进：
    - 区分「旧视频」和「新生成的视频」，避免误检
    - 先确认生成已启动（按钮 loading / 加载指示器），再检测完成
    """

    def __init__(self, page: Page, selectors, initial_video_count: int = 0):
        self.page = page
        self.sel = selectors
        self._start_time: float = 0
        self._last_status: Optional[GenerationStatus] = None
        self._last_percent: Optional[int] = None
        self._initial_video_count = initial_video_count
        self._generation_confirmed = False  # 是否确认生成已启动
        self._last_heartbeat_time: float = 0  # 上次心跳时间

    async def wait_for_completion(
        self, callback: Optional[ProgressCallback] = None
    ) -> ProgressInfo:
        """
        轮询等待视频生成完成。

        每次状态变化时调用 callback。
        返回最终的 ProgressInfo（COMPLETED 或 FAILED）。
        """
        self._start_time = time.time()
        self._last_heartbeat_time = self._start_time

        while True:
            info = await self._poll_once()

            # 只在状态变化时触发回调
            state_changed = (
                info.status != self._last_status
                or info.progress_percent != self._last_percent
            )
            if state_changed:
                self._last_status = info.status
                self._last_percent = info.progress_percent
                if callback:
                    await callback(info)

            # 终止状态
            if info.status in (GenerationStatus.COMPLETED, GenerationStatus.FAILED):
                return info

            # 定期心跳检测（每 HEARTBEAT_INTERVAL 秒）
            now = time.time()
            if now - self._last_heartbeat_time >= HEARTBEAT_INTERVAL:
                self._last_heartbeat_time = now

                # 页面健康检查
                page_ok = await self._check_page_health()
                if not page_ok:
                    return ProgressInfo(
                        status=GenerationStatus.FAILED,
                        error_type=ErrorType.UNKNOWN,
                        message="页面无响应，浏览器可能已崩溃。",
                    )

                # 强制打印心跳状态（即使状态未变化）
                elapsed_min = int((now - self._start_time) / 60)
                heartbeat_info = ProgressInfo(
                    status=GenerationStatus.GENERATING,
                    progress_percent=info.progress_percent,
                    message=f"[心跳] 已等待 {elapsed_min} 分钟，页面正常，继续等待...",
                )
                if callback:
                    await callback(heartbeat_info)

            await asyncio.sleep(POLL_INTERVAL)

    async def _poll_once(self) -> ProgressInfo:
        """单次轮询：按优先级尝试各检测策略"""
        elapsed = time.time() - self._start_time
        elapsed_int = int(elapsed)

        # === 阶段 1：等待生成启动 ===
        if not self._generation_confirmed:
            # 检查是否有错误（如敏感词拦截，可能在启动前就出现）
            error = await self._detect_error()
            if error:
                return error

            # 检查按钮是否变为 disabled/loading（说明已提交）
            button_state = await self._read_button_state()
            if button_state == "loading":
                self._generation_confirmed = True
                return ProgressInfo(
                    status=GenerationStatus.GENERATING,
                    message="生成已启动，等待渲染...",
                )

            # 检查加载指示器
            if await self._detect_loading():
                self._generation_confirmed = True
                return ProgressInfo(
                    status=GenerationStatus.GENERATING,
                    message="生成已启动，正在渲染...",
                )

            # 检查是否已有新视频（可能跳过了 loading 状态）
            if await self._detect_new_video():
                return ProgressInfo(
                    status=GenerationStatus.COMPLETED,
                    progress_percent=100,
                    message="视频生成完成!",
                )

            # 等待启动超时（60 秒内没有任何生成迹象）
            if elapsed > 60:
                return ProgressInfo(
                    status=GenerationStatus.FAILED,
                    error_type=ErrorType.UNKNOWN,
                    message="生成未能启动（等待超过 60 秒），请检查是否正确点击了生成按钮。",
                )

            return ProgressInfo(
                status=GenerationStatus.GENERATING,
                message=f"等待生成启动... ({elapsed_int}s)",
            )

        # === 阶段 2：生成已启动，等待完成 ===

        # 策略 1：超时检测（最先检查，防止被后续 return 跳过）
        if elapsed > GENERATION_TIMEOUT:
            return ProgressInfo(
                status=GenerationStatus.FAILED,
                error_type=ErrorType.RENDER_TIMEOUT,
                message=f"渲染超时 (已等待 {elapsed_int}s，上限 {GENERATION_TIMEOUT}s)",
            )

        # 策略 2：检查错误（快速失败）
        error = await self._detect_error()
        if error:
            return error

        # 策略 3：检查是否有新视频出现（完成）
        if await self._detect_new_video():
            return ProgressInfo(
                status=GenerationStatus.COMPLETED,
                progress_percent=100,
                message="视频生成完成!",
            )

        # 策略 4：读取进度百分比
        percent = await self._read_progress_percent()
        if percent is not None:
            return ProgressInfo(
                status=GenerationStatus.GENERATING,
                progress_percent=percent,
                message=f"视频渲染中 ({percent}%)...",
            )

        # 策略 5：检查按钮状态（disabled/loading = 正在生成）
        button_state = await self._read_button_state()
        if button_state == "loading":
            return ProgressInfo(
                status=GenerationStatus.GENERATING,
                message=f"视频渲染中... (已等待 {elapsed_int}s)",
            )

        # 策略 6：检查加载指示器
        if await self._detect_loading():
            return ProgressInfo(
                status=GenerationStatus.GENERATING,
                message=f"正在生成... (已等待 {elapsed_int}s)",
            )

        # 策略 7：按钮恢复正常 + 无加载指示 → 可能已完成但视频还没渲染到 DOM
        if button_state == "normal" and elapsed > 5:
            await asyncio.sleep(2)
            if await self._detect_new_video():
                return ProgressInfo(
                    status=GenerationStatus.COMPLETED,
                    progress_percent=100,
                    message="视频生成完成!",
                )

        # 默认：仍在生成
        return ProgressInfo(
            status=GenerationStatus.GENERATING,
            message=f"正在生成... (已等待 {elapsed_int}s)",
        )

    async def _detect_error(self) -> Optional[ProgressInfo]:
        """检查页面上的错误提示"""
        from errors.handler import ErrorHandler

        for selector in [self.sel.SENSITIVE_CONTENT_ALERT, self.sel.ERROR_TOAST, self.sel.ERROR_DIALOG]:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
                if count > 0:
                    for i in range(count):
                        el = locator.nth(i)
                        if await el.is_visible():
                            error_text = await el.text_content() or ""
                            error_text = error_text.strip()
                            if error_text:
                                error_type = ErrorHandler.classify_error(error_text)
                                return ProgressInfo(
                                    status=GenerationStatus.FAILED,
                                    error_type=error_type,
                                    message=f"错误: {error_text[:100]}",
                                )
            except Exception:
                continue
        return None

    async def _detect_new_video(self) -> bool:
        """检查是否有新增的视频元素（排除页面加载时已有的旧视频）"""
        try:
            locator = self.page.locator(self.sel.VIDEO_RESULT)
            current_count = await locator.count()

            if current_count > self._initial_video_count:
                # 确认新视频是可见的且有 src
                for i in range(self._initial_video_count, current_count):
                    try:
                        el = locator.nth(i)
                        if await el.is_visible():
                            src = await el.get_attribute("src")
                            if src:
                                return True
                            # 也检查 <source> 子元素
                            source = el.locator("source")
                            if await source.count() > 0:
                                src = await source.first.get_attribute("src")
                                if src:
                                    return True
                    except Exception:
                        continue
        except Exception:
            pass
        return False

    async def _read_progress_percent(self) -> Optional[int]:
        """尝试从 DOM 中读取进度百分比"""
        for selector in [self.sel.PROGRESS_TEXT, self.sel.PROGRESS_BAR]:
            try:
                locator = self.page.locator(selector)
                if await locator.count() == 0:
                    continue

                el = locator.first

                # 方法 A：从文本内容中提取百分比
                text = await el.text_content() or ""
                match = re.search(r'(\d+)\s*%', text)
                if match:
                    return int(match.group(1))

                # 方法 B：从 aria-valuenow 属性读取
                val = await el.get_attribute("aria-valuenow")
                if val and val.isdigit():
                    return int(val)

                # 方法 C：从 style width 中提取
                style = await el.get_attribute("style") or ""
                match = re.search(r'width:\s*(\d+(?:\.\d+)?)%', style)
                if match:
                    return int(float(match.group(1)))

            except Exception:
                continue
        return None

    async def _read_button_state(self) -> str:
        """检查生成按钮的状态"""
        try:
            locator = self.page.locator(self.sel.GENERATE_BUTTON)
            if await locator.count() > 0:
                btn = locator.first
                is_disabled = await btn.is_disabled()
                classes = await btn.get_attribute("class") or ""

                if is_disabled or "loading" in classes or "disabled" in classes:
                    return "loading"
                return "normal"
        except Exception:
            pass
        return "unknown"

    async def _detect_loading(self) -> bool:
        """检查是否有加载指示器可见"""
        try:
            locator = self.page.locator(self.sel.LOADING_INDICATOR)
            count = await locator.count()
            if count > 0:
                for i in range(count):
                    if await locator.nth(i).is_visible():
                        return True
        except Exception:
            pass
        return False

    async def _check_page_health(self) -> bool:
        """检查页面是否仍然可响应（心跳健康检查）"""
        try:
            result = await self.page.evaluate("1+1")
            return result == 2
        except Exception:
            return False
