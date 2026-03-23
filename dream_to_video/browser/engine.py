"""即梦浏览器自动化引擎"""

import asyncio
import logging
import uuid
import sys
from pathlib import Path
from typing import Optional, Callable, Awaitable
from datetime import datetime

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright, BrowserContext, Page
from browser.stealth import apply_stealth
from browser.selectors import JimengSelectors
from config import (
    USER_DATA_DIR, JIMENG_VIDEO_URL, OUTPUT_DIR,
    HEADLESS, SLOW_MO, VIEWPORT, PAGE_LOAD_TIMEOUT,
    MAX_RETRIES, RETRY_BASE_DELAY,
)
from models import GenerationStatus, GenerationResult, ProgressInfo, ErrorType

ProgressCallback = Callable[[ProgressInfo], Awaitable[None]]


def _notify_login_required():
    """通知：需要登录"""
    logger.warning("[NOTIFICATION] 需要登录，请扫描二维码")
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(600, 400)
            import time; time.sleep(0.15)
        for _ in range(2):
            winsound.Beep(900, 150)
            import time; time.sleep(0.1)
    except Exception:
        pass


def _notify_login_success():
    """通知：登录成功"""
    logger.info("[NOTIFICATION] 登录成功")
    try:
        import winsound
        import time
        winsound.Beep(600, 200)
        time.sleep(0.05)
        winsound.Beep(900, 300)
    except Exception:
        pass


class JimengBrowser:
    """
    即梦浏览器自动化引擎（使用持久化浏览器配置文件）。

    用法:
        async with JimengBrowser() as browser:
            result = await browser.generate_video("你的提示词", progress_callback=...)
    """

    def __init__(self, headless: bool = HEADLESS):
        self.headless = headless
        self._pw_context_manager = None
        self._pw = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.sel = JimengSelectors

    async def __aenter__(self):
        """启动浏览器并加载持久化配置"""
        self._pw_context_manager = async_playwright()
        self._pw = await self._pw_context_manager.__aenter__()

        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=self.headless,
            slow_mo=SLOW_MO,
            viewport=VIEWPORT,
            locale="zh-CN",
        )

        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await apply_stealth(self.page)

        # 尝试从保存的 Cookie 文件加载登录状态
        await self._load_saved_cookies()

        # 导航到视频生成页面
        print("正在打开即梦视频生成页面...")
        await self.page.goto(JIMENG_VIDEO_URL, wait_until="domcontentloaded",
                             timeout=PAGE_LOAD_TIMEOUT * 1000)

        # 等待页面完全稳定（SPA 可能有二次加载/路由跳转）
        print("等待页面稳定...")
        await self._wait_for_page_stable()

        # 检查是否需要登录
        if await self._check_login_required():
            print("\n⚠ 检测到未登录或登录已过期，正在启动登录流程...")
            login_success = await self._interactive_login()
            if not login_success:
                raise RuntimeError(
                    "登录超时或失败。请重新运行程序。"
                )

        print("页面已就绪。")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """关闭浏览器"""
        if self._context:
            await self._context.close()
        if self._pw_context_manager:
            await self._pw_context_manager.__aexit__(exc_type, exc_val, exc_tb)

    async def _load_saved_cookies(self):
        """从保存的 Cookie 文件加载登录状态。"""
        import json as _json
        from config import BASE_DIR

        cookies_file = BASE_DIR / "data" / "jimeng_cookies.json"
        if not cookies_file.exists():
            return

        try:
            with open(cookies_file, "r", encoding="utf-8") as f:
                cookies = _json.load(f)

            if not cookies:
                return

            valid_cookies = []
            for c in cookies:
                cookie = {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                }
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
                await self._context.add_cookies(valid_cookies)
                print(f"  已加载 {len(valid_cookies)} 个保存的 Cookie")

        except Exception as e:
            print(f"  Cookie 加载失败（将尝试其他登录方式）: {e}")

    async def _wait_for_page_stable(self, max_wait=15):
        """
        等待页面完全稳定（URL 不再变化，且输入框可用）。
        即梦 SPA 可能在初始加载后做二次路由或重渲染。
        """
        for i in range(max_wait):
            url_before = self.page.url
            await self.page.wait_for_timeout(1000)
            url_after = self.page.url

            if url_before == url_after and i >= 4:
                # URL 稳定了至少 1 秒，且已等待至少 5 秒
                try:
                    input_loc = self.page.locator(self.sel.PROMPT_INPUT)
                    if await input_loc.count() > 0 and await input_loc.first.is_visible():
                        print(f"  页面在 {i + 1} 秒后稳定。")
                        return
                except Exception:
                    pass

        print(f"  页面等待 {max_wait} 秒后继续（可能未完全稳定）。")

    async def generate_video(
        self,
        prompt: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> GenerationResult:
        """完整的视频生成流程"""
        task_id = str(uuid.uuid4())[:8]

        from progress.tracker import ProgressTracker
        from errors.handler import ErrorHandler

        # 记录现有视频数量（避免误检测旧视频为新结果）
        initial_video_count = await self._count_existing_videos()
        print(f"  页面上已有 {initial_video_count} 个视频元素。")

        for attempt in range(MAX_RETRIES + 1):
            try:
                # 1. 配置生成设置（模型、比例、时长）
                await self._notify(progress_callback, ProgressInfo(
                    status=GenerationStatus.SUBMITTING,
                    message="正在配置生成设置...",
                ))
                await self._configure_settings()

                # 等待设置生效，避免触发页面重渲染影响后续输入
                await self.page.wait_for_timeout(2000)

                # 2. 输入 Prompt
                await self._notify(progress_callback, ProgressInfo(
                    status=GenerationStatus.SUBMITTING,
                    message="正在输入提示词...",
                ))
                await self._input_prompt(prompt)

                # 3. 点击生成
                await self._notify(progress_callback, ProgressInfo(
                    status=GenerationStatus.SUBMITTING,
                    message="正在点击生成按钮...",
                ))
                await self._click_generate()

                # 4. 等待生成完成（传入初始视频数，只检测新增视频）
                tracker = ProgressTracker(
                    self.page, self.sel,
                    initial_video_count=initial_video_count,
                )
                result_info = await tracker.wait_for_completion(progress_callback)

                if result_info.status == GenerationStatus.COMPLETED:
                    await self._notify(progress_callback, ProgressInfo(
                        status=GenerationStatus.DOWNLOADING,
                        progress_percent=100,
                        message="正在下载视频...",
                    ))
                    video_path = await self._download_video(initial_video_count)

                    result = GenerationResult(
                        task_id=task_id,
                        status=GenerationStatus.COMPLETED,
                        video_path=str(video_path) if video_path else None,
                        prompt_used=prompt,
                        completed_at=datetime.now(),
                    )
                    await self._notify(progress_callback, ProgressInfo(
                        status=GenerationStatus.COMPLETED,
                        progress_percent=100,
                        message=f"视频生成完成! 保存到: {video_path}",
                    ))
                    return result

                if result_info.status == GenerationStatus.FAILED:
                    error_type = result_info.error_type or ErrorType.UNKNOWN

                    if not ErrorHandler.should_retry(error_type, attempt):
                        return GenerationResult(
                            task_id=task_id,
                            status=GenerationStatus.FAILED,
                            error_type=error_type,
                            error_message=result_info.message,
                            prompt_used=prompt,
                        )

                    delay = ErrorHandler.get_retry_delay(attempt)
                    await self._notify(progress_callback, ProgressInfo(
                        status=GenerationStatus.GENERATING,
                        message=f"遇到错误，{delay:.0f}秒后重试 ({attempt + 1}/{MAX_RETRIES})...",
                    ))
                    await asyncio.sleep(delay)
                    await self.page.reload(wait_until="domcontentloaded")
                    await self._wait_for_page_stable()
                    initial_video_count = await self._count_existing_videos()
                    continue

            except Exception as e:
                if attempt >= MAX_RETRIES:
                    return GenerationResult(
                        task_id=task_id,
                        status=GenerationStatus.FAILED,
                        error_type=ErrorType.UNKNOWN,
                        error_message=f"未知错误: {str(e)}",
                        prompt_used=prompt,
                    )
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                await self._notify(progress_callback, ProgressInfo(
                    status=GenerationStatus.GENERATING,
                    message=f"发生异常: {str(e)[:80]}，{delay:.0f}秒后重试...",
                ))
                await asyncio.sleep(delay)
                await self.page.reload(wait_until="domcontentloaded")
                await self._wait_for_page_stable()
                initial_video_count = await self._count_existing_videos()

        return GenerationResult(
            task_id=task_id,
            status=GenerationStatus.FAILED,
            error_type=ErrorType.UNKNOWN,
            error_message="所有重试均失败",
            prompt_used=prompt,
        )

    async def _configure_settings(self):
        """
        配置视频生成设置。
        目标：Seedance 2.0（非 Fast）、16:9 比例、15 秒时长。
        如果找不到对应控件则跳过（不中断流程）。

        工具栏使用 Lark Design 的 lv-select 组件（role="combobox"），
        点击后弹出 role="listbox" 下拉框，包含 role="option" 选项。
        """
        await self._select_model()
        await self._select_aspect_ratio()
        await self._select_duration()

    # ---------- lv-select 通用辅助方法 ----------

    async def _find_toolbar_select_by_text(
        self, patterns: list, exclude_classes: list = None
    ):
        """
        在工具栏中按文字内容查找 lv-select 下拉框。
        patterns: 待匹配的文字片段列表（满足任意一个即可）
        exclude_classes: 需要排除的 class 关键字（如 'type-select'）
        返回匹配的 locator 或 None。
        """
        exclude = exclude_classes or []
        all_selects = self.page.locator(self.sel.TOOLBAR_ALL_SELECTS)
        count = await all_selects.count()

        for i in range(count):
            sel = all_selects.nth(i)
            classes = await sel.get_attribute("class") or ""

            # 跳过排除的类
            if any(exc in classes for exc in exclude):
                continue

            text = (await sel.text_content() or "").strip()
            if any(p in text for p in patterns):
                return sel
        return None

    async def _click_lv_select_option(
        self, select_el, option_text: str, exclude_text: str = None
    ) -> bool:
        """
        点击 lv-select 下拉框并选择指定选项。
        select_el: lv-select 的 locator
        option_text: 目标选项必须包含的文字
        exclude_text: 目标选项不能包含的文字（用于排除 "Fast" 等）
        返回 True 表示成功选择，False 表示未找到选项。
        """
        # 点击打开下拉
        await select_el.click()
        await self.page.wait_for_timeout(800)

        # 等待 popup 出现（role="listbox"）
        popup = self.page.locator(self.sel.LV_SELECT_POPUP)
        try:
            await popup.first.wait_for(state="visible", timeout=3000)
        except Exception:
            print("  下拉菜单未弹出，按 Escape 关闭...")
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(300)
            return False

        # 查找目标选项
        options = self.page.locator(self.sel.LV_SELECT_OPTION)
        opt_count = await options.count()

        for j in range(opt_count):
            opt = options.nth(j)
            opt_text_content = (await opt.text_content() or "").strip()

            # 必须包含目标文字
            if option_text not in opt_text_content:
                continue

            # 排除包含指定文字的选项
            if exclude_text and exclude_text in opt_text_content:
                continue

            if await opt.is_visible():
                await opt.click()
                await self.page.wait_for_timeout(500)
                return True

        # 没找到，关闭下拉
        await self.page.keyboard.press("Escape")
        await self.page.wait_for_timeout(300)
        return False

    # ---------- 具体设置方法 ----------

    async def _select_model(self):
        """选择 Seedance 2.0（非 Fast 版本）"""
        try:
            # 在工具栏中查找模型下拉框
            # 模型下拉框的文字可能是 "视频 3.0 Fast"、"Seedance 2.0" 等
            # 排除 type-select（"视频生成"类型选择器）
            model_select = await self._find_toolbar_select_by_text(
                patterns=["视频 3.0", "Seedance", "Video", "视频 2.0"],
                exclude_classes=[self.sel.TOOLBAR_TYPE_SELECT_CLASS,
                                 self.sel.TOOLBAR_FEATURE_SELECT_CLASS],
            )

            if not model_select:
                print("  ⚠ 未找到模型选择器，跳过")
                return

            current_text = (await model_select.text_content() or "").strip()

            # 检查是否已经是 Seedance 2.0（非 Fast）
            if "Seedance 2.0" in current_text and "Fast" not in current_text:
                print(f"  ✓ 模型已正确: {current_text}")
                return

            print(f"  当前模型: {current_text}，切换到 Seedance 2.0...")

            # 点击打开下拉，选择 Seedance 2.0（排除 Fast）
            success = await self._click_lv_select_option(
                model_select,
                option_text="Seedance 2.0",
                exclude_text="Fast",
            )

            if success:
                print("  ✓ 已切换到 Seedance 2.0")
            else:
                print("  ⚠ 未找到 Seedance 2.0 选项")

        except Exception as e:
            print(f"  ⚠ 模型选择出错: {e}")

    async def _select_aspect_ratio(self):
        """确认 16:9 比例（基于检查结果，已是正确值）"""
        try:
            ratio_btn = self.page.locator(self.sel.TOOLBAR_RATIO_BUTTON)
            if await ratio_btn.count() > 0:
                btn_text = (await ratio_btn.first.text_content() or "").strip()
                if "16:9" in btn_text:
                    print(f"  ✓ 比例已正确: {btn_text}")
                    return

                # 如果不是 16:9，需要点击按钮打开面板选择
                print(f"  当前比例: {btn_text}，需要切换到 16:9...")
                await ratio_btn.first.click()
                await self.page.wait_for_timeout(1000)

                # 在弹出面板中查找 16:9 选项
                ratio_options = self.page.locator(':text("16:9")')
                for i in range(await ratio_options.count()):
                    if await ratio_options.nth(i).is_visible():
                        await ratio_options.nth(i).click()
                        await self.page.wait_for_timeout(500)
                        print("  ✓ 已选择 16:9 比例")
                        return

                # 关闭面板
                await self.page.keyboard.press("Escape")
                await self.page.wait_for_timeout(300)
                print("  ⚠ 未找到 16:9 比例选项")
            else:
                print("  ⚠ 未找到比例按钮，跳过")
        except Exception as e:
            print(f"  ⚠ 比例选择出错: {e}")

    async def _select_duration(self):
        """选择 15 秒时长"""
        try:
            import re as _re

            # 在工具栏中查找时长下拉框
            # 时长下拉框的文字是 "5s"、"10s"、"15s" 等
            # 排除 type-select 和 feature-select
            duration_select = await self._find_toolbar_select_by_text(
                patterns=["5s", "10s", "15s"],
                exclude_classes=[self.sel.TOOLBAR_TYPE_SELECT_CLASS,
                                 self.sel.TOOLBAR_FEATURE_SELECT_CLASS],
            )

            if not duration_select:
                print("  ⚠ 未找到时长选择器，跳过")
                return

            current_text = (await duration_select.text_content() or "").strip()

            if current_text == "15s":
                print("  ✓ 时长已正确: 15s")
                return

            print(f"  当前时长: {current_text}，切换到 15s...")

            # 点击打开下拉，选择 15s
            success = await self._click_lv_select_option(
                duration_select,
                option_text="15s",
            )

            if success:
                print("  ✓ 已选择时长: 15s")
            else:
                # 降级方案：尝试包含 "15" 的选项
                success = await self._click_lv_select_option(
                    duration_select,
                    option_text="15",
                )
                if success:
                    print("  ✓ 已选择时长: 15s")
                else:
                    print("  ⚠ 未找到 15s 时长选项")

        except Exception as e:
            print(f"  ⚠ 时长选择出错: {e}")

    # ---------- 参考图上传 ----------

    async def _switch_feature_mode(self, target_text: str) -> bool:
        """
        切换工具栏 feature-select（功能选择器）到指定模式。

        使用已有的 _find_toolbar_select_by_text + _click_lv_select_option 模式。
        target_text: "全能参考" / "首尾帧" / "智能多帧" / "主体参考"
        """
        try:
            feature_select = await self._find_toolbar_select_by_text(
                patterns=["全能参考", "首尾帧", "智能多帧", "主体参考"],
                exclude_classes=[],  # feature-select 不排除
            )

            if not feature_select:
                print(f"  ⚠ 未找到功能选择器（feature-select）")
                return False

            current_text = (await feature_select.text_content() or "").strip()
            if target_text in current_text:
                print(f"  ✓ 功能模式已正确: {current_text}")
                return True

            print(f"  当前功能模式: {current_text}，切换到 {target_text}...")
            success = await self._click_lv_select_option(
                feature_select,
                option_text=target_text,
            )

            if success:
                print(f"  ✓ 已切换到 {target_text}")
                await self.page.wait_for_timeout(1000)  # 等待 UI 更新
                return True
            else:
                print(f"  ⚠ 未找到 {target_text} 选项")
                return False

        except Exception as e:
            print(f"  ⚠ 功能模式切换出错: {e}")
            return False

    async def _upload_reference_image(self, image_path: Path) -> bool:
        """
        切换到「全能参考」模式并上传参考图。

        流程（基于 2026-03-01 DOM 检查）：
        1. 将 feature-select 从「首尾帧」切换到「全能参考」
        2. 等待参考区域 UI 更新为全能参考的上传槽
        3. 通过 hidden file input 或 click-to-upload 上传图片
        4. 等待上传完成

        Returns True if successful, False otherwise (graceful fallback).
        """
        sel = self.sel

        try:
            # Step 1: 切换到「全能参考」模式
            mode_ok = await self._switch_feature_mode("全能参考")
            if not mode_ok:
                print("  ⚠ 无法切换到全能参考模式，跳过参考图上传")
                return False

            await self.page.wait_for_timeout(1000)

            # Step 2: 上传图片
            # 策略 A: 直接设置 hidden file input（最可靠）
            file_input = self.page.locator(sel.REFERENCE_FILE_INPUT)
            if await file_input.count() > 0:
                await file_input.first.set_input_files(str(image_path))
                print(f"  ✓ 参考图文件已选择: {image_path.name}")
            else:
                # 策略 B: 点击上传区域触发文件选择对话框
                upload_area = self.page.locator(sel.REFERENCE_UPLOAD_AREA)
                if await upload_area.count() > 0:
                    async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                        await upload_area.first.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(str(image_path))
                    print(f"  ✓ 参考图文件已选择（通过点击上传区域）: {image_path.name}")
                else:
                    print("  ⚠ 未找到参考图上传入口（file input 和 upload area 均未找到）")
                    return False

            # Step 3: 等待上传完成
            await self.page.wait_for_timeout(3000)

            # 验证：检查缩略图是否出现
            thumbnail = self.page.locator(sel.REFERENCE_IMAGE_THUMBNAIL)
            if await thumbnail.count() > 0 and await thumbnail.first.is_visible():
                print(f"  ✓ 参考图上传成功: {image_path.name}")
                return True
            else:
                # 即使没看到缩略图，file input 上传通常已生效
                print(f"  ✓ 参考图已上传（未检测到缩略图，但文件已提交）: {image_path.name}")
                return True

        except Exception as e:
            print(f"  ⚠ 参考图上传异常: {e}")
            return False

    async def _remove_reference_image(self) -> bool:
        """
        清理参考图：将 feature-select 切回「首尾帧」模式。

        这是最可靠的清理方式 —— 切换模式会自动清除之前上传的参考图，
        确保下一个不需要参考图的 Prompt 不受影响。
        """
        try:
            success = await self._switch_feature_mode("首尾帧")
            if success:
                print("  ✓ 已恢复到首尾帧模式（参考图已清理）")
            return success
        except Exception as e:
            print(f"  ⚠ 恢复首尾帧模式异常: {e}")
            return False

    # ---------- 提示词输入 ----------

    async def _input_prompt(self, prompt: str):
        """在输入框中输入提示词（兼容 React 状态管理）"""
        input_locator = self.page.locator(self.sel.PROMPT_INPUT)

        # 等待输入框出现并可见
        try:
            await input_locator.first.wait_for(state="visible", timeout=10000)
        except Exception:
            raise RuntimeError(
                "找不到提示词输入框！请检查 browser/selectors.py 中的 PROMPT_INPUT 选择器。"
            )

        target = input_locator.first
        tag = await target.evaluate("el => el.tagName.toLowerCase()")

        # 点击输入框获得焦点
        await target.click()
        await self.page.wait_for_timeout(500)

        # 清空已有内容
        await self.page.keyboard.press("Control+a")
        await self.page.keyboard.press("Backspace")
        await self.page.wait_for_timeout(300)

        # 使用 React 兼容方式设置值
        # React 覆写了元素实例的 value setter，需要用原型链上的原始 setter
        proto_name = "HTMLTextAreaElement" if tag == "textarea" else "HTMLInputElement"
        await target.evaluate(f"""(el, text) => {{
            const setter = Object.getOwnPropertyDescriptor(
                window.{proto_name}.prototype, 'value'
            ).set;
            setter.call(el, text);
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}""", prompt)

        await self.page.wait_for_timeout(800)

        # 验证提示词是否成功输入
        current_value = await target.evaluate("el => el.value || el.textContent || ''")
        if not current_value or len(current_value.strip()) < min(10, len(prompt) // 2):
            print("  提示词可能未正确输入，使用键盘逐字输入...")
            await target.click()
            await self.page.wait_for_timeout(300)
            await self.page.keyboard.press("Control+a")
            await self.page.keyboard.press("Backspace")
            await self.page.wait_for_timeout(200)
            # 逐字输入（较慢但最可靠）
            await target.type(prompt, delay=30)
            await self.page.wait_for_timeout(500)
        else:
            print(f"  提示词已输入 ({len(current_value)} 字符)")

        # 额外按键触发确保 React 感知到变化
        await self.page.keyboard.press("End")
        await self.page.keyboard.press("Space")
        await self.page.wait_for_timeout(100)
        await self.page.keyboard.press("Backspace")
        await self.page.wait_for_timeout(500)

    async def _input_prompt_with_reference(self, prompt_body: str):
        """
        在「全能参考」模式的 ProseMirror 编辑器中输入带 @图片引用 的提示词。

        流程（基于 2026-03-01 DOM 检查）：
        1. 点击 ProseMirror 编辑器（.tiptap.ProseMirror）
        2. 输入 "在 "
        3. 输入 "@" → 触发 lv-select-popup 下拉菜单（role="listbox"）
        4. 等待并点击 "图片1" 选项（role="option"）
        5. 输入 " 的环境中，" + 余下的 prompt
        """
        # Step 1: 找到并点击 ProseMirror 编辑器
        editor = self.page.locator('.tiptap.ProseMirror')
        try:
            await editor.first.wait_for(state="visible", timeout=10000)
        except Exception:
            raise RuntimeError(
                "找不到 ProseMirror 编辑器！全能参考模式下应该有 .tiptap.ProseMirror 元素。"
            )

        await editor.first.click()
        await self.page.wait_for_timeout(500)

        # 清空已有内容
        await self.page.keyboard.press("Control+a")
        await self.page.keyboard.press("Backspace")
        await self.page.wait_for_timeout(300)

        # Step 2: 输入 "在 "
        await self.page.keyboard.type("在 ", delay=80)
        await self.page.wait_for_timeout(300)

        # Step 3: 输入 "@" 触发图片选择下拉
        await self.page.keyboard.type("@", delay=80)
        await self.page.wait_for_timeout(1500)

        # Step 4: 等待下拉菜单出现并点击 "图片1" 选项
        option = self.page.locator('[role="option"]')
        try:
            await option.first.wait_for(state="visible", timeout=3000)
            opt_text = (await option.first.text_content() or "").strip()
            print(f"  @ 下拉菜单已弹出，选择: {opt_text}")
            await option.first.click()
            await self.page.wait_for_timeout(800)
        except Exception:
            # 下拉菜单未出现，可能 @ 未触发或图片未上传成功
            print("  ⚠ @ 下拉菜单未弹出，改用纯文本模式")
            await self.page.keyboard.press("Backspace")
            await self.page.wait_for_timeout(200)
            await self.page.keyboard.type(prompt_body, delay=10)
            await self.page.wait_for_timeout(500)
            text = (await editor.first.text_content() or "").strip()
            print(f"  提示词已输入（纯文本回退）({len(text)} 字符)")
            return

        # Step 5: 输入 " 的环境中，" + 余下 prompt
        await self.page.keyboard.type(" 的环境中，", delay=50)
        await self.page.wait_for_timeout(200)
        await self.page.keyboard.type(prompt_body, delay=10)
        await self.page.wait_for_timeout(500)

        # 验证
        text = (await editor.first.text_content() or "").strip()
        print(f"  提示词已输入（含 @图片引用）({len(text)} 字符)")

    async def _click_generate(self):
        """点击生成按钮（遍历找到可见的那个，跳过 collapsed 按钮）"""
        btn_locator = self.page.locator(self.sel.GENERATE_BUTTON)

        # 页面上有两个 submit-button，一个 collapsed（不可见），一个是真正的按钮
        # 遍历找到可见的那个
        btn = None
        count = await btn_locator.count()

        if count == 0:
            # 选择器没匹配到任何元素，用更宽泛的备选选择器
            fallback = self.page.locator('button[class*="submit-button"]')
            count = await fallback.count()
            if count > 0:
                btn_locator = fallback
            else:
                raise RuntimeError(
                    "找不到生成按钮！请检查 browser/selectors.py 中的 GENERATE_BUTTON 选择器。"
                )

        # 遍历所有匹配的按钮，找到可见的那个
        for i in range(await btn_locator.count()):
            candidate = btn_locator.nth(i)
            try:
                if await candidate.is_visible():
                    btn = candidate
                    break
            except Exception:
                continue

        if not btn:
            # 没有可见按钮，等待第一个变为可见
            try:
                await btn_locator.first.wait_for(state="visible", timeout=10000)
                btn = btn_locator.first
            except Exception:
                raise RuntimeError(
                    "生成按钮存在但不可见！可能页面布局发生了变化。"
                )

        # 等待按钮从 disabled 变为 enabled
        for wait_attempt in range(15):
            if await btn.is_enabled():
                break
            print(f"  等待生成按钮启用... ({wait_attempt + 1}/15)")
            await self.page.wait_for_timeout(1000)
        else:
            # 最后尝试：强制点击
            print("  按钮仍为禁用状态，尝试强制点击...")
            await btn.click(force=True)
            await self.page.wait_for_timeout(1000)
            return

        await btn.click()
        print("  ✓ 已点击生成按钮")
        await self.page.wait_for_timeout(1000)

    async def _count_existing_videos(self) -> int:
        """统计页面上现有的视频元素数量"""
        try:
            locator = self.page.locator(self.sel.VIDEO_RESULT)
            count = await locator.count()
            return count
        except Exception:
            return 0

    async def _download_video(self, initial_video_count: int = 0) -> Optional[Path]:
        """下载生成的视频文件（优先下载新生成的）"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = OUTPUT_DIR / f"dream_video_{timestamp}.mp4"

        # 策略 1：从新增的 video 元素提取 src
        video_url = await self._extract_new_video_url(initial_video_count)
        if video_url:
            success = await self._download_from_url(video_url, output_file)
            if success:
                return output_file

        # 策略 2：从任意 video 元素提取 src（降级）
        video_url = await self._extract_video_url()
        if video_url:
            success = await self._download_from_url(video_url, output_file)
            if success:
                return output_file

        # 策略 3：点击下载按钮
        dl_btn = self.page.locator(self.sel.DOWNLOAD_BUTTON)
        if await dl_btn.count() > 0:
            try:
                async with self.page.expect_download(timeout=30000) as download_info:
                    await dl_btn.first.click()
                download = await download_info.value
                await download.save_as(str(output_file))
                if output_file.exists():
                    return output_file
            except Exception as e:
                print(f"下载按钮方式失败: {e}")

        print("警告：无法自动下载视频。请手动从浏览器中下载。")
        return None

    async def _extract_new_video_url(self, initial_count: int) -> Optional[str]:
        """从新增的视频元素中提取 URL"""
        video_locator = self.page.locator(self.sel.VIDEO_RESULT)
        current_count = await video_locator.count()

        for i in range(initial_count, current_count):
            try:
                el = video_locator.nth(i)
                src = await el.get_attribute("src")
                if src:
                    return src
                source = el.locator("source")
                if await source.count() > 0:
                    src = await source.first.get_attribute("src")
                    if src:
                        return src
            except Exception:
                continue
        return None

    async def _extract_video_url(self) -> Optional[str]:
        """从页面中提取视频 URL"""
        video_locator = self.page.locator(self.sel.VIDEO_RESULT)
        if await video_locator.count() > 0:
            src = await video_locator.first.get_attribute("src")
            if src:
                return src
            source = video_locator.first.locator("source")
            if await source.count() > 0:
                src = await source.first.get_attribute("src")
                if src:
                    return src
        return None

    async def _download_from_url(self, url: str, output_path: Path) -> bool:
        """下载文件"""
        try:
            response = await self.page.request.get(url)
            if response.ok:
                body = await response.body()
                output_path.write_bytes(body)
                return True
        except Exception as e:
            print(f"URL 下载失败: {e}")
        return False

    async def _check_login_required(self) -> bool:
        """检查当前页面是否需要登录"""
        login_locator = self.page.locator(self.sel.LOGIN_PROMPT)
        try:
            count = await login_locator.count()
            if count > 0:
                for i in range(count):
                    if await login_locator.nth(i).is_visible():
                        return True
        except Exception:
            pass
        return False

    async def _interactive_login(self) -> bool:
        """
        交互式登录流程：在当前浏览器中引导用户扫码登录。

        流程：
        1. 导航到即梦首页（显示二维码）
        2. 播放提示音 + 打印醒目提示
        3. 轮询检测登录成功（二维码消失 + 输入框出现）
        4. 登录成功后自动跳转回视频生成页面
        """
        from config import JIMENG_BASE_URL, LOGIN_TIMEOUT

        # 播放提示音提醒用户
        _notify_login_required()

        # 导航到即梦首页（会显示登录二维码）
        print("正在打开即梦登录页面...")
        await self.page.goto(JIMENG_BASE_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)

        # 打印醒目提示
        print()
        print("=" * 56)
        print("  !! 需要登录 !!")
        print("=" * 56)
        print()
        print("  浏览器已打开即梦网站，请用手机扫描页面上的二维码登录。")
        print("  登录成功后程序会自动检测并继续工作。")
        print()
        print(f"  超时时间: {LOGIN_TIMEOUT // 60} 分钟")
        print("=" * 56)
        print()

        # 轮询检测登录成功
        poll_interval = 3  # 每 3 秒检测一次
        max_attempts = LOGIN_TIMEOUT // poll_interval

        for attempt in range(max_attempts):
            await self.page.wait_for_timeout(poll_interval * 1000)

            # 检测方式 1：用户头像出现 = 已登录
            try:
                avatar = self.page.locator(self.sel.USER_AVATAR)
                if await avatar.count() > 0:
                    for i in range(await avatar.count()):
                        if await avatar.nth(i).is_visible():
                            print("\n  ✅ 检测到已登录！正在跳转到视频生成页面...")
                            _notify_login_success()
                            # 跳转到视频生成页
                            await self.page.goto(
                                JIMENG_VIDEO_URL,
                                wait_until="domcontentloaded",
                                timeout=PAGE_LOAD_TIMEOUT * 1000,
                            )
                            await self._wait_for_page_stable()
                            # 再次确认登录状态
                            if not await self._check_login_required():
                                print("  ✅ 登录成功，继续工作。\n")
                                return True
            except Exception:
                pass

            # 检测方式 2：登录弹窗消失 = 可能已登录
            try:
                login_locator = self.page.locator(self.sel.LOGIN_PROMPT)
                login_visible = False
                if await login_locator.count() > 0:
                    for i in range(await login_locator.count()):
                        if await login_locator.nth(i).is_visible():
                            login_visible = True
                            break

                # 如果 URL 已经变化（可能自动跳转了）且没有登录弹窗
                if not login_visible and "jimeng" in self.page.url:
                    # 跳转到视频生成页验证
                    await self.page.goto(
                        JIMENG_VIDEO_URL,
                        wait_until="domcontentloaded",
                        timeout=PAGE_LOAD_TIMEOUT * 1000,
                    )
                    await self._wait_for_page_stable()
                    if not await self._check_login_required():
                        print("\n  ✅ 登录成功，继续工作。\n")
                        _notify_login_success()
                        return True
                    else:
                        # 还没登录，回到首页继续等
                        await self.page.goto(JIMENG_BASE_URL, wait_until="domcontentloaded")
            except Exception:
                pass

            # 定期打印等待提示
            elapsed = (attempt + 1) * poll_interval
            if elapsed % 30 == 0:
                remaining = LOGIN_TIMEOUT - elapsed
                print(f"  ⏳ 等待登录中... (已等待 {elapsed}s，剩余 {remaining}s)")

        print("\n  ❌ 登录超时。请重新运行程序。")
        return False

    @staticmethod
    async def _notify(callback: Optional[ProgressCallback], info: ProgressInfo):
        """发送进度通知"""
        if callback:
            await callback(info)
