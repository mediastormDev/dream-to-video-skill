"""即梦平台登录与认证持久化（使用持久化浏览器配置文件）"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from browser.stealth import apply_stealth
from config import USER_DATA_DIR, JIMENG_BASE_URL, JIMENG_VIDEO_URL, LOGIN_TIMEOUT, VIEWPORT


async def save_auth_state():
    """
    使用持久化浏览器配置文件登录。
    登录状态会保存在 browser_profile 目录中，像真实 Chrome 一样保留所有信息。
    """
    print("=" * 50)
    print("  即梦平台 - 扫码登录")
    print("=" * 50)
    print()
    print("接下来会自动打开浏览器，请用手机扫码登录。")
    print(f"超时时间: {LOGIN_TIMEOUT} 秒（约 {LOGIN_TIMEOUT // 60} 分钟）")
    print()

    # 确保配置文件目录存在
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # 使用 launch_persistent_context：浏览器配置会保存在 USER_DATA_DIR 中
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            slow_mo=50,
            viewport=VIEWPORT,
            locale="zh-CN",
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await apply_stealth(page)

        # 导航到即梦
        print("正在打开即梦网站...")
        await page.goto(JIMENG_BASE_URL, wait_until="domcontentloaded")
        print()
        print(">>> 浏览器已打开！请用手机扫描页面上的二维码登录 <<<")
        print(">>> 登录成功后程序会自动检测，你只需要等着就行 <<<")
        print()

        # 等待用户登录：不设自动检测超时，完全由用户手动确认
        print("请在浏览器中完成登录（扫码或其他方式）。")
        print("登录成功后，你能在页面上看到自己的头像。")
        print()
        print("====================================")
        print("  登录成功后，回到这里按 Enter 键继续")
        print("====================================")
        await asyncio.get_event_loop().run_in_executor(None, input)

        # 访问视频生成页面，确保相关 Cookie 被写入
        print("正在保存登录状态...")
        await page.goto(JIMENG_VIDEO_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        print()
        print(f"登录状态已保存到: {USER_DATA_DIR}")
        print()
        print("以后运行时会自动使用保存的登录状态，无需重复扫码。")
        print("如果登录过期了，重新运行: python main.py login")

        await context.close()


async def verify_auth() -> bool:
    """验证登录状态是否有效"""
    if not USER_DATA_DIR.exists():
        print("未找到浏览器配置，请先运行: python main.py login")
        return False

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=True,
            viewport=VIEWPORT,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await apply_stealth(page)

        await page.goto(JIMENG_VIDEO_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # 检查是否需要登录
        login_prompts = page.locator(
            '[class*="login-modal"], [class*="qr-code"], '
            '[class*="login-dialog"], [class*="sign-in"]'
        )
        is_expired = await login_prompts.count() > 0

        await context.close()

        if is_expired:
            print("登录已过期，请重新运行: python main.py login")
            return False

        return True


if __name__ == "__main__":
    asyncio.run(save_auth_state())
