"""
本地登录工具：在你的电脑上弹出浏览器登录即梦，然后自动把 Cookie 发送到服务器。

用法：
    python tools/export_cookies.py <服务器地址> <Admin Token>

例如：
    python tools/export_cookies.py http://47.236.58.83:8080 Xushi8888
"""

import asyncio
import json
import sys
import urllib.request
import urllib.error


async def main():
    if len(sys.argv) < 3:
        print("用法: python tools/export_cookies.py <服务器地址> <Admin Token>")
        print("例如: python tools/export_cookies.py http://47.236.58.83:8080 Xushi8888")
        sys.exit(1)

    server_url = sys.argv[1].rstrip("/")
    admin_token = sys.argv[2]

    print("=" * 50)
    print("  即梦登录 Cookie 导出工具")
    print("=" * 50)
    print()
    print("接下来会打开浏览器，请正常登录即梦。")
    print("登录成功后，Cookie 会自动发送到服务器。")
    print()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("需要安装 playwright：")
        print("  pip install playwright")
        print("  playwright install chromium")
        sys.exit(1)

    async with async_playwright() as p:
        # 启动可见浏览器
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = await context.new_page()

        # 打开即梦
        print("正在打开即梦网站...")
        await page.goto("https://jimeng.jianying.com/ai-tool/generate?type=video",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        print()
        print(">>> 浏览器已打开！请登录即梦（扫码或其他方式）<<<")
        print(">>> 登录成功后回到这里按 Enter 键 <<<")
        print()

        # 等待用户登录
        await asyncio.get_event_loop().run_in_executor(None, input, "登录完成后按 Enter 继续...")

        # 确保访问视频页面以获取完整 Cookie
        await page.goto("https://jimeng.jianying.com/ai-tool/generate?type=video",
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # 提取所有 Cookie
        cookies = await context.cookies()
        print(f"\n提取到 {len(cookies)} 个 Cookie")

        # 过滤即梦相关的 Cookie
        jimeng_cookies = [c for c in cookies
                         if "jianying" in c.get("domain", "")
                         or "jimeng" in c.get("domain", "")
                         or "douyin" in c.get("domain", "")
                         or "bytedance" in c.get("domain", "")
                         or "byteimg" in c.get("domain", "")]

        if not jimeng_cookies:
            # 如果过滤后没有，就发送所有 Cookie
            jimeng_cookies = cookies
            print(f"发送所有 {len(jimeng_cookies)} 个 Cookie")
        else:
            print(f"过滤出 {len(jimeng_cookies)} 个即梦相关 Cookie")

        await browser.close()

        # 发送到服务器
        print(f"\n正在发送 Cookie 到 {server_url}...")

        cookie_data = json.dumps(jimeng_cookies, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{server_url}/admin/upload-cookies",
            data=cookie_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {admin_token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                print()
                print("=" * 50)
                print(f"  {result.get('message', '上传成功！')}")
                print("=" * 50)
                print()
                print("现在可以正常使用 Dream-to-Video 了！")
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"上传失败 (HTTP {e.code}): {body}")
        except Exception as e:
            print(f"上传失败: {e}")

            # 保存到本地文件作为备份
            backup_file = "jimeng_cookies.json"
            with open(backup_file, "w", encoding="utf-8") as f:
                json.dump(jimeng_cookies, f, ensure_ascii=False, indent=2)
            print(f"\nCookie 已保存到本地文件: {backup_file}")
            print("你可以手动上传到服务器的 admin 页面。")


if __name__ == "__main__":
    asyncio.run(main())
