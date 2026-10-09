"""Douyin MCP Server — 讓 AI 透過瀏覽器刷抖音、按讚、留言、發佈作品、做推薦。

以 Playwright 操作抖音網頁版（非官方 API），登入狀態保存在本機 profile 目錄。
"""
import asyncio
import json
import os
import random
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from playwright.async_api import Page, async_playwright

PROFILE_DIR = Path(os.getenv("DOUYIN_PROFILE_DIR", "~/.douyin-mcp/profile")).expanduser()
STATE_FILE = PROFILE_DIR.parent / "state.json"
HEADLESS = os.getenv("DOUYIN_HEADLESS", "0") == "1"
MIN_INTERVAL = float(os.getenv("DOUYIN_MIN_INTERVAL", "20"))  # 留言/發佈最短間隔（秒）
LIMITS = {
    "comment": int(os.getenv("DOUYIN_DAILY_COMMENT_LIMIT", "20")),
    "publish": int(os.getenv("DOUYIN_DAILY_PUBLISH_LIMIT", "3")),
}

HOME_URL = "https://www.douyin.com/?recommend=1"
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"

mcp = FastMCP("douyin")

_pw = None
_ctx = None
_page: Page | None = None
_lock = asyncio.Lock()
_last_action = {"comment": 0.0, "publish": 0.0}


# ---------- 基礎工具 ----------
async def get_page() -> Page:
    global _pw, _ctx, _page
    if _ctx is None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _pw = await async_playwright().start()
        _ctx = await _pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()
    return _page


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _check_and_count(kind: str) -> str | None:
    """超過每日上限或間隔太短回傳錯誤訊息；否則計數並回傳 None。"""
    loop_time = asyncio.get_event_loop().time()
    wait = MIN_INTERVAL - (loop_time - _last_action[kind])
    if _last_action[kind] and wait > 0:
        return f"操作太頻繁，請 {wait:.0f} 秒後再試。"
    state = _load_state()
    today = str(date.today())
    day = state.get(today, {})
    if day.get(kind, 0) >= LIMITS[kind]:
        return f"今日 {kind} 已達上限 {LIMITS[kind]}（可用環境變數調整）。"
    day[kind] = day.get(kind, 0) + 1
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({today: day}))
    _last_action[kind] = loop_time
    return None


async def _first_text(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count():
                text = (await el.inner_text(timeout=1500)).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


async def _video_info(page: Page) -> dict:
    scope = 'div[data-e2e="feed-active-video"]'
    return {
        "url": page.url,
        "author": await _first_text(page, [f"{scope} [data-e2e='feed-video-nickname']", "[data-e2e='feed-video-nickname']"]),
        "description": await _first_text(page, [f"{scope} [data-e2e='video-desc']", "[data-e2e='video-desc']"]),
        "likes": await _first_text(page, [f"{scope} [data-e2e='video-player-digg']", "[data-e2e='video-player-digg']"]),
        "comments": await _first_text(page, [f"{scope} [data-e2e='feed-comment-icon']", "[data-e2e='feed-comment-icon']"]),
    }


# ---------- 瀏覽 ----------
@mcp.tool()
async def open_douyin(wait_for_login: bool = False) -> str:
    """開啟抖音推薦頁。第一次使用請把 wait_for_login 設 True，並在彈出的瀏覽器手動登入。"""
    async with _lock:
        page = await get_page()
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        if wait_for_login:
            await page.wait_for_selector("[data-e2e='live-avatar'], [data-e2e='user-info']", timeout=180_000)
        return "已開啟抖音推薦頁。" + ("登入完成。" if wait_for_login else "")


@mcp.tool()
async def current_video() -> dict:
    """取得目前正在播放的影片資訊（作者、描述、讚數、留言數、網址）。"""
    async with _lock:
        return await _video_info(await get_page())


@mcp.tool()
async def next_video(watch_seconds: float = 8) -> dict:
    """先停留 watch_seconds 秒（會加一點隨機），再切換到下一支影片並回傳其資訊。"""
    async with _lock:
        page = await get_page()
        await asyncio.sleep(max(0.0, watch_seconds) + random.uniform(0, 2))
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(1.5)
        return await _video_info(page)


@mcp.tool()
async def previous_video() -> dict:
    """回到上一支影片。"""
    async with _lock:
        page = await get_page()
        await page.keyboard.press("ArrowUp")
        await asyncio.sleep(1.5)
        return await _video_info(page)


@mcp.tool()
async def browse_for_recommendations(count: int = 5, watch_seconds: float = 6) -> list[dict]:
    """連續刷 count 支影片（上限 20）並回傳每支的資訊，供 AI 整理推薦清單。"""
    count = max(1, min(count, 20))
    results = []
    async with _lock:
        page = await get_page()
        for _ in range(count):
            results.append(await _video_info(page))
            await asyncio.sleep(max(0.0, watch_seconds) + random.uniform(0, 2))
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(1.5)
    return results


# ---------- 互動 ----------
@mcp.tool()
async def like_video() -> str:
    """對目前影片按讚（快捷鍵 Z）。再按一次會取消讚。"""
    async with _lock:
        page = await get_page()
        await page.keyboard.press("z")
        return "已送出按讚操作。"


@mcp.tool()
async def read_comments(limit: int = 10) -> list[str]:
    """開啟目前影片的留言區並讀取前 limit 則留言文字。"""
    async with _lock:
        page = await get_page()
        await page.keyboard.press("x")
        await asyncio.sleep(2)
        items = page.locator("[data-e2e='comment-item']")
        n = min(await items.count(), max(1, limit))
        return [(await items.nth(i).inner_text()).strip() for i in range(n)]


@mcp.tool()
async def post_comment(text: str) -> str:
    """在目前影片底下留言。有每日上限與最短間隔限制，避免被判定為洗版。"""
    if not text.strip():
        return "留言內容不可為空。"
    async with _lock:
        err = _check_and_count("comment")
        if err:
            return err
        page = await get_page()
        await page.keyboard.press("x")
        await asyncio.sleep(1.5)
        box = page.locator(
            "[data-e2e='comment-input'] [contenteditable='true'], div[contenteditable='true']"
        ).first
        await box.click()
        await page.keyboard.type(text, delay=random.randint(40, 120))
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)
        return "已送出留言。"


# ---------- 發佈 ----------
@mcp.tool()
async def publish_video(video_path: str, caption: str) -> str:
    """從創作者中心上傳本機影片並發佈。video_path 需為本機絕對路徑。"""
    path = Path(video_path).expanduser()
    if not path.is_file():
        return f"找不到檔案：{path}"
    async with _lock:
        err = _check_and_count("publish")
        if err:
            return err
        page = await get_page()
        await page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        await page.set_input_files("input[type='file']", str(path))
        # 等待上傳完成（出現「重新上传」字樣），最久 10 分鐘
        await page.get_by_text("重新上传").first.wait_for(timeout=600_000)
        editor = page.locator("div[contenteditable='true']").first
        await editor.click()
        await page.keyboard.type(caption, delay=random.randint(30, 80))
        await asyncio.sleep(1)
        await page.get_by_role("button", name="发布", exact=True).click()
        await asyncio.sleep(3)
        return "已點擊發佈，請到創作者中心確認狀態。"


def main():
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
