"""Playwright 反自动化检测"""

import sys
from playwright.async_api import Page
from playwright_stealth import Stealth

# 根据运行平台选择 navigator.platform 伪装值
_platform_override = "Linux x86_64" if sys.platform != "win32" else "Win32"

# 全局 Stealth 实例，配置中文语言环境
_stealth = Stealth(
    navigator_languages_override=("zh-CN", "zh"),
    navigator_platform_override=_platform_override,
)


async def apply_stealth(page: Page):
    """对单个页面应用反检测补丁"""
    await _stealth.apply_stealth_async(page)
