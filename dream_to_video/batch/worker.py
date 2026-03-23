"""
视频生成 Worker：长时间运行的后台进程。

职责：
1. 打开浏览器，保持登录
2. 定期检查队列文件，提交新 prompt
3. 监控页面，检测已完成的视频
4. 自动下载完成的视频
5. 对下载的视频执行后处理特效（elliptic-shatter）
6. 每 10 分钟打印心跳状态
"""

import asyncio
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 后处理特效脚本路径
EFFECT_SCRIPT = Path(__file__).parent.parent / "effects" / "elliptic_shatter.py"
EFFECT_NAME = "elliptic-shatter"

# elliptic-shatter 默认参数
EFFECT_PARAMS = [
    "--inner-edge", "0.95",
    "--outer-edge", "1.28",
    "--ellipse-y", "0.82",
    "--max-disp", "70",
    "--edge-brightness", "0.92",
    "--ca", "5",
    "--grain", "18",
]


def _notify_download_complete(task_id: str):
    """通知：下载完成"""
    logger.info(f"[NOTIFICATION] Task {task_id} 下载完成")
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(800, 200)
            time.sleep(0.1)
    except Exception:
        pass


def _notify_moderation_failed(task_id: str):
    """通知：审核未通过"""
    logger.warning(f"[NOTIFICATION] Task {task_id} 审核未通过")
    try:
        import winsound
        for _ in range(5):
            winsound.Beep(1200, 150)
            time.sleep(0.05)
            winsound.Beep(400, 150)
            time.sleep(0.05)
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))

from browser.engine import JimengBrowser
from browser.selectors import JimengSelectors
from batch.persistence import (
    get_new_prompts,
    save_batch_state,
    load_batch_state,
    mark_task_processed,
)
from config import (
    WORKER_POLL_INTERVAL,
    GLOBAL_TIMEOUT,
    SUBMIT_INTERVAL,
    HEARTBEAT_INTERVAL,
    OUTPUT_DIR,
    MAX_MODERATION_RETRIES,
)
from models import BatchState, BatchTask, GenerationStatus
from browser.reference_image import (
    needs_reference_image,
    classify_scene,
    select_reference_image,
)


class VideoWorker:
    """
    视频生成 Worker。

    用法:
        worker = VideoWorker()
        await worker.run()
    """

    def __init__(self):
        self.browser: Optional[JimengBrowser] = None
        self.state: BatchState = load_batch_state() or BatchState()
        self._start_time: float = 0
        self._last_heartbeat: float = 0

    async def run(self):
        """Worker 主入口"""
        self._start_time = time.time()
        self._last_heartbeat = self._start_time
        self.state.worker_started_at = datetime.now()
        # 每次启动新 Worker 都重新配置设置（浏览器新开页面，toolbar 需要重新交互）
        self.state.settings_configured = False
        save_batch_state(self.state)

        print(flush=True)
        print("=" * 50, flush=True)
        print("  Dream-to-Video Worker 已启动", flush=True)
        print("=" * 50, flush=True)
        print(f"  已有任务: {len(self.state.tasks)} 个", flush=True)
        print(f"  全局超时: {GLOBAL_TIMEOUT // 3600} 小时", flush=True)
        print(f"  监控间隔: {WORKER_POLL_INTERVAL} 秒", flush=True)
        print(f"  心跳间隔: {HEARTBEAT_INTERVAL // 60} 分钟", flush=True)
        print(flush=True)

        try:
            async with JimengBrowser() as browser:
                self.browser = browser
                print("  浏览器已就绪，开始工作循环...\n")

                while True:
                    # 检查全局超时
                    elapsed = time.time() - self._start_time
                    if elapsed > GLOBAL_TIMEOUT:
                        print(f"\n⏰ 全局超时 ({GLOBAL_TIMEOUT // 3600}h)，Worker 退出。")
                        self._timeout_remaining()
                        break

                    # 1. 检查队列，提交新任务
                    await self._process_queue()

                    # 2. 检查已提交任务的完成情况
                    await self._check_completions()

                    # 3. 检查审核未通过（在结果卡片上显示）
                    await self._check_moderation_failures()

                    # 4. 心跳
                    now = time.time()
                    if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
                        self._last_heartbeat = now
                        await self._heartbeat()

                    # 5. 检查是否所有任务都已完成且队列为空
                    if self._all_done():
                        print("\n✅ 所有任务已完成，Worker 退出。")
                        break

                    await asyncio.sleep(WORKER_POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n\nWorker 被用户中断。")
        except Exception as e:
            print(f"\n❌ Worker 异常: {e}")
        finally:
            save_batch_state(self.state)
            self._print_summary()

    # ---------- 队列处理 ----------

    async def _process_queue(self):
        """检查队列文件中的新 prompt 并提交"""
        new_prompts = get_new_prompts(self.state)
        if not new_prompts:
            return

        print(f"\n📥 发现 {len(new_prompts)} 个新任务")

        for prompt_data in new_prompts:
            try:
                await self._submit_one(prompt_data)
            except Exception as e:
                print(f"  ❌ 提交失败: {e}")
                task = BatchTask(
                    task_id=prompt_data["task_id"],
                    prompt=prompt_data["prompt"],
                    status=GenerationStatus.FAILED,
                    submit_order=len(self.state.tasks),
                    error_message=str(e),
                )
                self.state.tasks.append(task)
                save_batch_state(self.state)
                # 标记为已处理（即使失败也不再重复提交）
                mark_task_processed(prompt_data["task_id"])

    async def _submit_one(self, prompt_data: dict):
        """提交单个 prompt 到即梦（不等待结果）"""
        # 首次提交前配置设置
        if not self.state.settings_configured:
            print("  正在配置生成设置...")
            await self.browser._configure_settings()
            await self.browser.page.wait_for_timeout(2000)
            self.state.settings_configured = True

        # 记录提交前的视频数（仅首次）
        if self.state.initial_video_count == 0 and not any(
            t.status in (GenerationStatus.SUBMITTED, GenerationStatus.COMPLETED)
            for t in self.state.tasks
        ):
            self.state.initial_video_count = await self.browser._count_existing_videos()
            print(f"  页面初始视频数: {self.state.initial_video_count}")

        task_id = prompt_data["task_id"]
        prompt = prompt_data["prompt"]
        prompt_preview = prompt[:50] + ("..." if len(prompt) > 50 else "")

        print(f"\n  📝 [{task_id}] 正在提交: {prompt_preview}")

        # ====== 预备参考图信息（Rule 10）======
        reference_image_used = None
        _needs_ref = needs_reference_image(prompt)
        _ref_image_path = None
        if _needs_ref:
            scene_type = classify_scene(prompt)
            _ref_image_path = select_reference_image(scene_type)
            if _ref_image_path:
                print(f"  🖼 [{task_id}] 检测到参考图需求 ({scene_type})，待上传: {_ref_image_path.name}")
            else:
                print(f"  ⚠ [{task_id}] {scene_type} 文件夹无可用参考图")
                _needs_ref = False

        try:
            if _needs_ref and _ref_image_path:
                # ====== 参考图模式：上传图 → 配置 → ProseMirror 输入（带 @引用）======
                # 全能参考模式下无 textarea，改用 ProseMirror 编辑器 + @图片引用。
                # 流程：切全能参考 → 上传图 → 重新配置(模型/比例/时长) → @引用输入
                for attempt in range(3):
                    try:
                        print(f"  🖼 [{task_id}] 上传参考图: {_ref_image_path.name}")
                        upload_ok = await self.browser._upload_reference_image(_ref_image_path)
                        if upload_ok:
                            reference_image_used = str(_ref_image_path)
                            # 切换到全能参考会重置模型/比例/时长
                            print(f"  🔧 全能参考模式下重新配置设置...")
                            await self.browser._configure_settings()
                            await self.browser.page.wait_for_timeout(1000)
                            # 在 ProseMirror 中输入 "在 @图片1 的环境中，{prompt_body}"
                            from config import REFERENCE_IMAGE_PREFIX
                            prompt_body = prompt[len(REFERENCE_IMAGE_PREFIX):]
                            await self.browser._input_prompt_with_reference(prompt_body)
                        else:
                            print(f"  ⚠ [{task_id}] 参考图上传失败，回退到普通模式")
                            await self.browser._remove_reference_image()
                            await self.browser._input_prompt(prompt)
                        break
                    except RuntimeError as e:
                        if attempt < 2:
                            print(f"  ⚠ 输入异常，刷新页面后重试 ({attempt + 1}/3)...")
                            from config import JIMENG_VIDEO_URL, PAGE_LOAD_TIMEOUT
                            await self.browser.page.goto(
                                JIMENG_VIDEO_URL,
                                wait_until="domcontentloaded",
                                timeout=PAGE_LOAD_TIMEOUT * 1000,
                            )
                            await self.browser._wait_for_page_stable(max_wait=20)
                            if await self.browser._check_login_required():
                                print("  ⚠ 检测到需要重新登录...")
                                login_ok = await self.browser._interactive_login()
                                if not login_ok:
                                    raise RuntimeError("登录失败，无法提交任务")
                            await self.browser._configure_settings()
                            await self.browser.page.wait_for_timeout(2000)
                        else:
                            raise
            else:
                # ====== 普通模式：textarea 输入 ======
                for attempt in range(3):
                    try:
                        await self.browser._input_prompt(prompt)
                        break
                    except RuntimeError as e:
                        if "找不到提示词输入框" in str(e) and attempt < 2:
                            print(f"  ⚠ 输入框未找到，刷新页面后重试 ({attempt + 1}/3)...")
                            from config import JIMENG_VIDEO_URL, PAGE_LOAD_TIMEOUT
                            await self.browser.page.goto(
                                JIMENG_VIDEO_URL,
                                wait_until="domcontentloaded",
                                timeout=PAGE_LOAD_TIMEOUT * 1000,
                            )
                            await self.browser._wait_for_page_stable(max_wait=20)
                            if await self.browser._check_login_required():
                                print("  ⚠ 检测到需要重新登录...")
                                login_ok = await self.browser._interactive_login()
                                if not login_ok:
                                    raise RuntimeError("登录失败，无法提交任务")
                            await self.browser._configure_settings()
                            await self.browser.page.wait_for_timeout(2000)
                        else:
                            raise

            # 提交前：标记页面上已有的同 prompt 视频 URL，防止监控误匹配旧结果
            await self._mark_pre_existing_videos(prompt)

            # 点击生成
            await self.browser._click_generate()

            # 等待提交注册
            await self.browser.page.wait_for_timeout(int(SUBMIT_INTERVAL * 1000))

            # 检查即时错误
            error = await self._check_immediate_error()
            if error:
                task = BatchTask(
                    task_id=task_id,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    submit_order=len(self.state.tasks),
                    submitted_at=datetime.now(),
                    error_message=error,
                    reference_image_path=reference_image_used,
                )
                self.state.tasks.append(task)
                save_batch_state(self.state)
                mark_task_processed(task_id)
                print(f"  ❌ [{task_id}] 提交失败: {error}")
                return

            # 记录任务
            task = BatchTask(
                task_id=task_id,
                prompt=prompt,
                status=GenerationStatus.SUBMITTED,
                submit_order=len(self.state.tasks),
                submitted_at=datetime.now(),
                reference_image_path=reference_image_used,
            )
            self.state.tasks.append(task)
            save_batch_state(self.state)
            # 标记为已处理（防止状态文件损坏后重复提交）
            mark_task_processed(task_id)
            print(f"  ✓ [{task_id}] 已提交到即梦")

        finally:
            # ====== 清理参考图（无论成功失败都执行）======
            if reference_image_used:
                try:
                    await self.browser._remove_reference_image()
                except Exception as e:
                    print(f"  ⚠ 参考图清理异常: {e}")

    async def _mark_pre_existing_videos(self, prompt: str):
        """
        提交前扫描页面，标记已有的同 prompt 视频 URL 为"已下载"。

        当同一 prompt 重复提交时（如 task_007 → task_008），
        页面上会同时存在旧结果和新结果的卡片。监控按 prompt 文本匹配时
        可能把旧视频误认为新结果。此方法在点击"生成"前将旧 URL 标记，
        确保监控只会下载真正的新视频。
        """
        sel = self.browser.sel
        cards = self.browser.page.locator(sel.GENERATION_RESULT_CARD)
        try:
            card_count = await cards.count()
        except Exception:
            return

        snippet = prompt[:20]
        # 参考图模式下，页面显示 "在 图片1 的环境中，..." 而非原始前缀
        body_snippet = None
        if needs_reference_image(prompt):
            from config import REFERENCE_IMAGE_PREFIX
            body_snippet = prompt[len(REFERENCE_IMAGE_PREFIX):][:20]
        marked = 0

        for i in range(card_count):
            try:
                card = cards.nth(i)
                card_text = (await card.text_content() or "").strip()
                if snippet not in card_text and (not body_snippet or body_snippet not in card_text):
                    continue

                video_el = card.locator("video")
                if await video_el.count() == 0:
                    continue

                url = await video_el.first.get_attribute("src")
                if not url:
                    source = video_el.first.locator("source")
                    if await source.count() > 0:
                        url = await source.first.get_attribute("src")

                if url and url.startswith("http") and url not in self.state.downloaded_video_urls:
                    self.state.downloaded_video_urls.append(url)
                    marked += 1
            except Exception:
                continue

        if marked:
            save_batch_state(self.state)
            print(f"  📋 已标记 {marked} 个已有视频 URL（防止误匹配旧结果）")

    async def _check_immediate_error(self) -> Optional[str]:
        """检查提交后是否有即时错误（敏感词等）"""
        sel = self.browser.sel
        for _ in range(3):
            for error_sel in [sel.SENSITIVE_CONTENT_ALERT, sel.ERROR_TOAST, sel.ERROR_DIALOG]:
                try:
                    locator = self.browser.page.locator(error_sel)
                    if await locator.count() > 0:
                        for i in range(await locator.count()):
                            if await locator.nth(i).is_visible():
                                text = (await locator.nth(i).text_content() or "").strip()
                                if text:
                                    return text
                except Exception:
                    pass
            await self.browser.page.wait_for_timeout(1000)
        return None

    # ---------- 完成检测 ----------

    async def _check_completions(self):
        """
        检查页面上是否有新完成的视频。

        策略：基于结果卡片容器（record-list-container）+ prompt 文本匹配。
        即梦的新结果出现在页面顶部（最新在前），所以不能依赖索引顺序，
        必须通过 prompt 文本来匹配视频和任务的对应关系。
        """
        if not self._has_pending_tasks():
            return

        sel = self.browser.sel

        # 获取所有待处理的任务
        submitted_tasks = [
            t for t in self.state.tasks
            if t.status == GenerationStatus.SUBMITTED
        ]
        if not submitted_tasks:
            return

        # 策略 1：基于结果卡片容器匹配（最可靠）
        found_any = await self._check_via_containers(submitted_tasks)

        # 策略 2：如果容器匹配失败，回退到扫描所有视频元素
        if not found_any:
            await self._check_via_all_videos(submitted_tasks)

    async def _check_via_containers(self, submitted_tasks: list) -> bool:
        """
        通过每个生成结果的独立卡片检测完成的视频。

        重要：即梦使用虚拟列表，record-list-container 是共享的单个容器，
        不是每个结果一个！真正的单个结果卡片是 ai-generated-record-content 元素。
        """
        sel = self.browser.sel

        # 先滚动虚拟列表以确保所有结果都在 DOM 中
        try:
            await self.browser.page.evaluate("""() => {
                // 找到虚拟列表容器并滚动到底再回顶
                const vlist = document.querySelector('[class*="virtual-list"]');
                if (vlist) {
                    vlist.scrollTop = vlist.scrollHeight;
                }
            }""")
            await self.browser.page.wait_for_timeout(800)
            await self.browser.page.evaluate("""() => {
                const vlist = document.querySelector('[class*="virtual-list"]');
                if (vlist) { vlist.scrollTop = 0; }
            }""")
            await self.browser.page.wait_for_timeout(500)
        except Exception:
            pass

        # 使用单个结果卡片选择器（每个生成任务一个）
        cards = self.browser.page.locator(sel.GENERATION_RESULT_CARD)

        try:
            card_count = await cards.count()
        except Exception:
            return False

        if card_count == 0:
            return False

        found_any = False
        # 首次扫描时打印诊断信息
        if not hasattr(self, '_debug_scan_done'):
            self._debug_scan_done = True
            print(f"\n  🔍 [诊断] 页面上共 {card_count} 个独立结果卡片", flush=True)
            for di in range(min(card_count, 10)):
                try:
                    dc = cards.nth(di)
                    dt = (await dc.text_content() or "").strip()[:60]
                    dv = await dc.locator("video").count()
                    print(f"    卡片[{di}]: video={dv}, text=\"{dt}\"", flush=True)
                except Exception as e:
                    print(f"    卡片[{di}]: 读取失败 {e}", flush=True)
            for t in submitted_tasks:
                print(f"    待匹配: [{t.task_id}] \"{t.prompt[:30]}...\"", flush=True)

        for i in range(card_count):
            try:
                card = cards.nth(i)

                # 获取卡片中的文字内容（单个卡片文本较短，不需要截断）
                card_text = (await card.text_content() or "").strip()
                if not card_text:
                    continue

                # 匹配到任务
                # ⚠ 参考图模式下，页面显示 "在 图片1 的环境中，..." 而非原始前缀
                #    所以额外用前缀后的 prompt 正文来匹配
                matched_task = None
                for task in submitted_tasks:
                    snippet = task.prompt[:20]
                    if snippet in card_text:
                        matched_task = task
                        break
                    # 参考图前缀替换后的匹配
                    if needs_reference_image(task.prompt):
                        from config import REFERENCE_IMAGE_PREFIX
                        body_snippet = task.prompt[len(REFERENCE_IMAGE_PREFIX):][:20]
                        if body_snippet in card_text:
                            matched_task = task
                            break

                if not matched_task:
                    continue

                # 跳过审核未通过的卡片（由 _check_moderation_failures 单独处理）
                moderation_keywords = ["审核未通过", "审核失败", "未通过审核", "内容违规"]
                if any(kw in card_text for kw in moderation_keywords):
                    continue

                # 检查卡片中是否有已完成的视频
                video_el = card.locator("video")
                if await video_el.count() == 0:
                    continue  # 可能还在生成中

                # 提取视频 URL
                url = await video_el.first.get_attribute("src")
                if not url:
                    source = video_el.first.locator("source")
                    if await source.count() > 0:
                        url = await source.first.get_attribute("src")

                if not url or url in self.state.downloaded_video_urls:
                    continue

                # 验证 URL 格式：必须是有效的 HTTP(S) URL
                if not url.startswith("http://") and not url.startswith("https://"):
                    continue  # 跳过 blob: URL、data: URL 等无效格式

                # 找到匹配的新视频！下载
                found_any = True
                print(f"\n  🎬 [{matched_task.task_id}] 视频已生成，正在下载...")
                video_path = await self._download_video(url, matched_task.task_id)

                if video_path:
                    matched_task.status = GenerationStatus.COMPLETED
                    matched_task.video_path = str(video_path)
                    matched_task.completed_at = datetime.now()
                    self.state.downloaded_video_urls.append(url)
                    save_batch_state(self.state)
                    print(f"  ✅ [{matched_task.task_id}] 原版下载完成: {video_path}", flush=True)

                    # 后处理特效
                    effect_path = await self._post_process_video(video_path, matched_task.task_id)
                    if effect_path:
                        matched_task.effect_video_path = str(effect_path)
                        save_batch_state(self.state)

                    _notify_download_complete(matched_task.task_id)
                    submitted_tasks.remove(matched_task)
                else:
                    print(f"  ⚠ [{matched_task.task_id}] 下载失败，将在下次循环重试")

            except Exception:
                continue

        return found_any

    async def _check_via_all_videos(self, submitted_tasks: list):
        """回退策略：扫描所有视频元素（不限制索引范围）"""
        sel = self.browser.sel
        video_locator = self.browser.page.locator(sel.VIDEO_RESULT)

        try:
            current_count = await video_locator.count()
        except Exception:
            return

        if current_count <= self.state.initial_video_count:
            return  # 总数没变化

        # 扫描所有视频（即梦新结果在顶部，不能只查尾部）
        for i in range(current_count):
            url = await self._extract_url_at(video_locator, i)
            if not url or url in self.state.downloaded_video_urls:
                continue

            # 验证 URL 格式
            if not url.startswith("http://") and not url.startswith("https://"):
                continue

            # 通过 prompt 文本匹配
            task = await self._match_by_prompt_text(video_locator, i)
            if not task:
                continue

            # 下载
            print(f"\n  🎬 [{task.task_id}] 视频已生成（回退匹配），正在下载...")
            video_path = await self._download_video(url, task.task_id)

            if video_path:
                task.status = GenerationStatus.COMPLETED
                task.video_path = str(video_path)
                task.completed_at = datetime.now()
                self.state.downloaded_video_urls.append(url)
                save_batch_state(self.state)
                print(f"  ✅ [{task.task_id}] 原版下载完成: {video_path}", flush=True)

                # 后处理特效
                effect_path = await self._post_process_video(video_path, task.task_id)
                if effect_path:
                    task.effect_video_path = str(effect_path)
                    save_batch_state(self.state)

                _notify_download_complete(task.task_id)
                if task in submitted_tasks:
                    submitted_tasks.remove(task)
            else:
                print(f"  ⚠ [{task.task_id}] 下载失败，将在下次循环重试")

    # ---------- 审核未通过检测 ----------

    async def _check_moderation_failures(self):
        """
        检测结果卡片上的"审核未通过"标识。

        当即梦平台内容审核拒绝视频时，对应结果卡片上会显示"审核未通过"文字。
        检测到后：
        1. 播放警告提示音
        2. 打印醒目警告
        3. 如果未超过最大重试次数，自动重新提交同一 prompt
        """
        submitted_tasks = [
            t for t in self.state.tasks
            if t.status == GenerationStatus.SUBMITTED
        ]
        if not submitted_tasks:
            return

        sel = self.browser.sel
        cards = self.browser.page.locator(sel.GENERATION_RESULT_CARD)

        try:
            card_count = await cards.count()
        except Exception:
            return

        if card_count == 0:
            return

        for i in range(card_count):
            try:
                card = cards.nth(i)
                card_text = (await card.text_content() or "").strip()
                if not card_text:
                    continue

                # 检查是否包含审核未通过标识
                moderation_keywords = ["审核未通过", "审核失败", "未通过审核", "内容违规"]
                is_moderation_failed = any(kw in card_text for kw in moderation_keywords)
                if not is_moderation_failed:
                    continue

                # 匹配到哪个任务
                matched_task = None
                for task in submitted_tasks:
                    snippet = task.prompt[:20]
                    if snippet in card_text:
                        matched_task = task
                        break

                if not matched_task:
                    continue

                # ✦ 检测到审核未通过！
                print(flush=True)
                print("!" * 56, flush=True)
                print(f"  ⚠️  [{matched_task.task_id}] 审核未通过！", flush=True)
                print(f"  提示词片段: {matched_task.prompt[:60]}...", flush=True)
                print(f"  当前重试次数: {matched_task.retry_count}/{MAX_MODERATION_RETRIES}", flush=True)
                print("!" * 56, flush=True)
                print(flush=True)

                # 播放警告提示音
                _notify_moderation_failed(matched_task.task_id)

                # 标记任务失败
                matched_task.status = GenerationStatus.FAILED
                matched_task.error_message = "审核未通过"
                matched_task.completed_at = datetime.now()
                save_batch_state(self.state)

                # 自动重试（如果未超过最大重试次数）
                if matched_task.retry_count < MAX_MODERATION_RETRIES:
                    await self._resubmit_task(matched_task)
                else:
                    print(f"  ❌ [{matched_task.task_id}] 已达最大重试次数 ({MAX_MODERATION_RETRIES})，"
                          f"不再自动重试。", flush=True)
                    print(f"     请考虑修改提示词后重新提交。", flush=True)

                # 从待检查列表中移除
                submitted_tasks.remove(matched_task)

            except Exception as e:
                continue

    async def _resubmit_task(self, failed_task: BatchTask):
        """
        重新提交审核未通过的任务。

        创建一个新任务条目（复用原始 prompt），retry_count +1。
        """
        new_retry_count = failed_task.retry_count + 1
        print(f"  🔄 [{failed_task.task_id}] 正在自动重新提交... "
              f"(第 {new_retry_count} 次重试)", flush=True)

        try:
            # 等待一段时间再重试（避免频繁提交触发风控）
            wait_seconds = 10 * new_retry_count  # 第 1 次等 10s，第 2 次等 20s
            print(f"  ⏳ 等待 {wait_seconds} 秒后重新提交...", flush=True)
            await asyncio.sleep(wait_seconds)

            # 构造重新提交的数据
            prompt_data = {
                "task_id": f"{failed_task.task_id}_retry{new_retry_count}",
                "prompt": failed_task.prompt,
            }

            # 直接调用提交方法
            await self._submit_one(prompt_data)

            # 提交成功后，更新新任务的 retry_count
            # 新任务是 state.tasks 中最后一个
            for task in reversed(self.state.tasks):
                if task.task_id == prompt_data["task_id"]:
                    task.retry_count = new_retry_count
                    save_batch_state(self.state)
                    break

            print(f"  ✓ [{prompt_data['task_id']}] 重新提交成功", flush=True)

        except Exception as e:
            print(f"  ❌ 重新提交失败: {e}", flush=True)

    async def _extract_url_at(self, video_locator, index: int) -> Optional[str]:
        """提取指定索引的视频 URL"""
        try:
            el = video_locator.nth(index)
            if not await el.is_visible():
                return None

            # 直接 src
            src = await el.get_attribute("src")
            if src:
                return src

            # <source> 子元素
            source = el.locator("source")
            if await source.count() > 0:
                src = await source.first.get_attribute("src")
                if src:
                    return src
        except Exception:
            pass
        return None

    def _match_to_task(self, new_video_offset: int) -> Optional[BatchTask]:
        """
        将新视频匹配到对应的任务。
        策略 1：按提交顺序匹配（第 N 个新视频 → 第 N 个已提交任务）
        策略 2：兜底，返回第一个未完成的任务
        """
        submitted_tasks = sorted(
            [t for t in self.state.tasks if t.status == GenerationStatus.SUBMITTED],
            key=lambda t: t.submit_order,
        )

        if new_video_offset < len(submitted_tasks):
            return submitted_tasks[new_video_offset]

        # 兜底：返回第一个未完成的任务
        for task in submitted_tasks:
            return task
        return None

    async def _match_by_prompt_text(self, video_locator, index: int) -> Optional[BatchTask]:
        """
        通过 prompt 文本匹配视频到任务。
        即梦的 ai-generated-record-content 包含 prompt 原文，
        可从视频元素向上查找到包含文本的容器。
        """
        try:
            el = video_locator.nth(index)
            # 向上查找包含 prompt 文本的最近生成结果容器
            card_text = await el.evaluate("""el => {
                let parent = el;
                for (let d = 0; d < 15; d++) {
                    parent = parent.parentElement;
                    if (!parent) break;
                    const cls = parent.className || '';
                    if (cls.includes('ai-generated-record-content') || cls.includes('video-record-nlt') || cls.includes('slot-card-container')) {
                        return (parent.textContent || '').trim().substring(0, 500);
                    }
                }
                return '';
            }""")

            if not card_text:
                return None

            # 用 prompt 前 20 字符去匹配
            submitted_tasks = [
                t for t in self.state.tasks
                if t.status == GenerationStatus.SUBMITTED
            ]
            for task in submitted_tasks:
                snippet = task.prompt[:20]
                if snippet in card_text:
                    return task
                # 参考图前缀替换后的匹配
                if needs_reference_image(task.prompt):
                    from config import REFERENCE_IMAGE_PREFIX
                    body_snippet = task.prompt[len(REFERENCE_IMAGE_PREFIX):][:20]
                    if body_snippet in card_text:
                        return task
        except Exception:
            pass
        return None

    async def _download_video(self, url: str, task_id: str) -> Optional[Path]:
        """下载视频到 output/ 目录"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = OUTPUT_DIR / f"{task_id}_{timestamp}.mp4"

        try:
            response = await self.browser.page.request.get(url)
            if response.ok:
                body = await response.body()
                output_file.write_bytes(body)
                if output_file.exists() and output_file.stat().st_size > 0:
                    return output_file
        except Exception as e:
            print(f"    下载失败: {e}")

        return None

    async def _post_process_video(self, video_path: Path, task_id: str) -> Optional[Path]:
        """
        对下载的视频执行 elliptic-shatter 后处理特效。

        在子进程中运行（CPU 密集型），不阻塞 async 事件循环。
        返回处理后的视频路径，失败返回 None。
        """
        if not EFFECT_SCRIPT.exists():
            print(f"  ⚠ [{task_id}] 特效脚本不存在: {EFFECT_SCRIPT}", flush=True)
            return None

        # 输出文件名：在原文件名基础上加特效名称后缀
        stem = video_path.stem  # e.g. "task_002_20260227_220822"
        effect_file = video_path.parent / f"{stem}_{EFFECT_NAME}.mp4"

        print(f"  🔮 [{task_id}] 正在执行后处理特效 ({EFFECT_NAME})...", flush=True)

        cmd = [
            sys.executable, str(EFFECT_SCRIPT),
            "--input", str(video_path),
            "--output", str(effect_file),
            *EFFECT_PARAMS,
        ]

        try:
            # 在子进程中运行，不阻塞事件循环
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            # 实时读取输出并打印进度
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").rstrip()
                if line_str:
                    print(f"    {line_str}", flush=True)

            await proc.wait()

            if proc.returncode == 0 and effect_file.exists() and effect_file.stat().st_size > 0:
                print(f"  ✨ [{task_id}] 特效处理完成: {effect_file}", flush=True)
                return effect_file
            else:
                print(f"  ⚠ [{task_id}] 特效处理失败 (exit code: {proc.returncode})", flush=True)
                return None

        except Exception as e:
            print(f"  ⚠ [{task_id}] 特效处理异常: {e}", flush=True)
            return None

    # ---------- 心跳与健康检查 ----------

    async def _heartbeat(self):
        """打印心跳状态 + 页面诊断"""
        elapsed_min = int((time.time() - self._start_time) / 60)

        total = len(self.state.tasks)
        completed = sum(1 for t in self.state.tasks if t.status == GenerationStatus.COMPLETED)
        submitted = sum(1 for t in self.state.tasks if t.status == GenerationStatus.SUBMITTED)
        failed = sum(1 for t in self.state.tasks if t.status == GenerationStatus.FAILED)
        moderation_failed = sum(1 for t in self.state.tasks
                                if t.status == GenerationStatus.FAILED
                                and t.error_message == "审核未通过")

        print(f"\n  💓 [心跳] 已运行 {elapsed_min} 分钟 | "
              f"总计: {total} | 完成: {completed} | 等待: {submitted} | 失败: {failed}"
              + (f" (审核未通过: {moderation_failed})" if moderation_failed else ""),
              flush=True)

        # 页面诊断：检查视频和容器数量
        try:
            sel = self.browser.sel
            video_count = await self.browser.page.locator(sel.VIDEO_RESULT).count()
            card_count = await self.browser.page.locator(sel.GENERATION_RESULT_CARD).count()
            print(f"  💓 页面状态: video元素={video_count}, 结果卡片={card_count}, "
                  f"初始视频数={self.state.initial_video_count}, "
                  f"已下载URL={len(self.state.downloaded_video_urls)}",
                  flush=True)
        except Exception as e:
            print(f"  💓 页面诊断失败: {e}", flush=True)

        # 页面健康检查
        try:
            result = await self.browser.page.evaluate("1+1")
            if result != 2:
                raise RuntimeError("页面返回异常")
            print(f"  💓 页面响应正常", flush=True)
        except Exception:
            print(f"  ⚠ 页面无响应，尝试刷新...", flush=True)
            try:
                await self.browser.page.reload(wait_until="domcontentloaded")
                await self.browser._wait_for_page_stable()
                save_batch_state(self.state)
                print(f"  ✓ 页面已刷新", flush=True)
            except Exception as e:
                print(f"  ❌ 页面刷新失败: {e}", flush=True)

    # ---------- 辅助方法 ----------

    def _has_pending_tasks(self) -> bool:
        """是否有正在等待完成的任务"""
        return any(t.status == GenerationStatus.SUBMITTED for t in self.state.tasks)

    def _all_done(self) -> bool:
        """所有任务是否已完成（且队列中无新任务）"""
        if not self.state.tasks:
            return False  # 还没有任何任务，继续等待

        new = get_new_prompts(self.state)
        if new:
            return False  # 队列中还有新任务

        return all(
            t.status in (GenerationStatus.COMPLETED, GenerationStatus.FAILED)
            for t in self.state.tasks
        )

    def _timeout_remaining(self):
        """将所有未完成的任务标记为超时"""
        for task in self.state.tasks:
            if task.status == GenerationStatus.SUBMITTED:
                task.status = GenerationStatus.FAILED
                task.error_message = "全局超时"
        save_batch_state(self.state)

    def _print_summary(self):
        """打印最终汇总"""
        print()
        print("=" * 50)
        print("  Worker 任务汇总")
        print("=" * 50)

        for task in self.state.tasks:
            icon = "✅" if task.status == GenerationStatus.COMPLETED else "❌"
            path = task.video_path or task.error_message or "未完成"
            prompt_preview = task.prompt[:40] + ("..." if len(task.prompt) > 40 else "")
            print(f"  {icon} [{task.task_id}] {prompt_preview}")
            print(f"     原版 → {path}")
            if hasattr(task, 'effect_video_path') and task.effect_video_path:
                print(f"     特效 → {task.effect_video_path}")

        completed = sum(1 for t in self.state.tasks if t.status == GenerationStatus.COMPLETED)
        print(f"\n  完成: {completed}/{len(self.state.tasks)}")
        print()


async def run_worker():
    """Worker 入口函数"""
    worker = VideoWorker()
    await worker.run()
