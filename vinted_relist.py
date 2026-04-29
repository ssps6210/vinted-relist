#!/usr/bin/env python3
"""
vinted_relist.py
自動備份並重新上架 vinted.co.uk 的所有商品。

流程分三個 Phase：
  Phase 1 — 備份：抓取所有商品資料 + 下載圖片 → backup/
  Phase 2 — 刪除：逐一刪除原本的商品
  Phase 3 — 重新上架：自動填入圖片/標題/描述/價格，
             Category / Brand / Size / Condition 需手動選擇

注意：每個 Phase 完成後會暫停詢問是否繼續，讓你可以隨時中斷。
"""

import asyncio
import json
import random
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from playwright.async_api import async_playwright, Page

# ── 設定 ──────────────────────────────────────────────────────────────────────
with open("config.json", encoding="utf-8") as _f:
    _cfg = json.load(_f)

EMAIL    = _cfg["email"]
PASSWORD = _cfg["password"]
BASE     = "https://www.vinted.co.uk"
BACKUP   = Path("backup")
HEADLESS = False


def jitter(lo=3.0, hi=7.0) -> float:
    return random.uniform(lo, hi)


def confirm(prompt: str) -> bool:
    ans = input(f"\n{prompt} [y/n]: ").strip().lower()
    return ans in ("y", "yes", "")


COOKIE_FILE = Path("cookies.json")


async def save_cookies(page: Page) -> None:
    cookies = await page.context.cookies()
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
    print("  ✓ Cookie 已儲存，下次不需要重新登入")


async def load_cookies(page: Page) -> bool:
    if not COOKIE_FILE.exists():
        return False
    cookies = json.loads(COOKIE_FILE.read_text())
    await page.context.add_cookies(cookies)
    return True


# ── 登入 ─────────────────────────────────────────────────────────────────────
async def login(page: Page) -> None:
    print("→ 開啟 Vinted…")

    # 嘗試用儲存的 cookie 自動登入
    if await load_cookies(page):
        await page.goto(BASE, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        not_logged_in = await page.locator('a[href*="signup"], a[href*="login"]').count()
        if not not_logged_in:
            print("  ✓ 自動登入成功（使用儲存的 session）")
            return
        print("  Cookie 已過期，需要重新登入")

    # 手動登入
    await page.goto(
        "https://www.vinted.co.uk/member/signup/select_type?ref_url=%2F",
        wait_until="domcontentloaded"
    )
    print()
    print("  請在瀏覽器中手動完成登入：")
    print("  1. 點擊 'Log in'")
    print("  2. 輸入帳號密碼")
    print("  3. 確認已登入首頁後，回到這裡按 Enter")
    input("  → 登入完成後按 Enter：")
    await page.wait_for_load_state("networkidle")
    await save_cookies(page)
    print("  ✓ 登入成功")


# ── 收集商品 URL ───────────────────────────────────────────────────────────────
async def collect_urls(page: Page) -> list[str]:
    print("→ 請在瀏覽器中前往你的 Wardrobe（個人商品頁面）")
    print("  例如：點擊右上角頭像 → 'My wardrobe' 或 'Sell'")
    print("  到達你的商品列表頁後回到這裡按 Enter")
    input("  → 按 Enter 開始收集商品連結：")

    current_url = page.url
    print(f"  目前頁面：{current_url}")

    # 滾動到底部讓所有商品載入
    prev = 0
    for _ in range(30):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        links = await page.eval_on_selector_all('a[href*="/items/"]', "els => els.map(e => e.href)")
        if len(links) == prev:
            break
        prev = len(links)

    # 只保留商品詳細頁（結尾是數字 ID，排除 /items/new）
    urls = sorted({
        l for l in links
        if l.split("?")[0].rstrip("/").split("/")[-1].isdigit()
    })
    print(f"  找到 {len(urls)} 件商品")
    return urls


# ── 抓取單一商品資料 ─────────────────────────────────────────────────────────
async def scrape_item(page: Page, url: str) -> dict | None:
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            break
        except Exception:
            if attempt == 2:
                print(f"  ✗ 頁面載入失敗，略過")
                return None
            await asyncio.sleep(5)
    await asyncio.sleep(jitter(2, 4))

    # 跳過已售出的商品
    sold_indicators = [
        '[data-testid="item-status--sold"]',
        'text="Sold"',
        '[class*="sold"]',
        'text="Reserved"',
    ]
    for sel in sold_indicators:
        if await page.locator(sel).count():
            print("  ↷ 已售出，略過")
            return None

    try:
        title = await page.locator("h1").first.inner_text()

        desc_loc = page.locator('[itemprop="description"]')
        description = await desc_loc.inner_text() if await desc_loc.count() else ""

        price_loc = page.locator('[data-testid="item-price"]')
        price = await price_loc.inner_text() if await price_loc.count() else ""

        photos: list[str] = await page.eval_on_selector_all(
            '[data-testid="item-photo"] img, .item-photos img, [class*="photo"] img',
            "els => [...new Set(els.map(e => e.src))].filter(s => s.startsWith('http'))"
        )

        return {
            "url": url,
            "title": title.strip(),
            "description": description.strip(),
            "price": price.strip(),
            "photos": photos,
        }
    except Exception as e:
        print(f"  ✗ 抓取失敗 {url}: {e}")
        return None


# ── Phase 1：備份 ─────────────────────────────────────────────────────────────
async def phase_backup(page: Page, urls: list[str]) -> list[dict]:
    BACKUP.mkdir(exist_ok=True)
    items = []

    for i, url in enumerate(urls, 1):
        item_id = url.rstrip("/").split("/")[-1]
        item_dir = BACKUP / item_id
        item_dir.mkdir(exist_ok=True)
        json_file = item_dir / "data.json"

        if json_file.exists():
            print(f"  [{i}/{len(urls)}] 已備份，跳過：{item_id}")
            items.append(json.loads(json_file.read_text()))
            continue

        print(f"  [{i}/{len(urls)}] 抓取 {item_id}…", end=" ", flush=True)
        data = await scrape_item(page, url)
        if not data:
            continue

        # 下載圖片
        local_photos: list[str] = []
        for j, src in enumerate(data["photos"]):
            dest = item_dir / f"photo_{j}.jpg"
            try:
                urllib.request.urlretrieve(src, dest)
                local_photos.append(str(dest))
            except Exception as e:
                print(f"\n    圖片 {j} 下載失敗：{e}")
        data["local_photos"] = local_photos

        json_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        items.append(data)
        print(f"✓  ({len(local_photos)} 張圖)")
        await asyncio.sleep(jitter())

    return items


# ── Phase 2：刪除 ─────────────────────────────────────────────────────────────
async def delete_item(page: Page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(jitter(2, 4))

        # 嘗試開啟選單（⋯ 或 More options）
        for sel in [
            '[data-testid="item-more-options"]',
            'button[aria-label*="more" i]',
            'button[aria-label*="options" i]',
            '[data-testid="item-actions"] button',
        ]:
            btn = page.locator(sel)
            if await btn.count():
                await btn.first.click()
                await asyncio.sleep(1.5)
                break

        # 點擊 Delete
        for sel in ['[data-testid="delete-item"]', 'button:has-text("Delete")', 'a:has-text("Delete")']:
            btn = page.locator(sel)
            if await btn.count():
                await btn.first.click()
                await asyncio.sleep(1.5)
                break

        # 等彈窗出現再確認刪除（紅色 Confirm 按鈕）
        await asyncio.sleep(2)
        for sel in [
            'button:has-text("Confirm")',
            '[data-testid="confirm-delete"]',
            '[role="dialog"] button:has-text("Delete")',
            '[role="dialog"] button:has-text("Confirm")',
        ]:
            btn = page.locator(sel)
            if await btn.count():
                await btn.first.click()
                await asyncio.sleep(3)
                print("    ✓ 已刪除")
                return

    except Exception as e:
        print(f"    ✗ 自動刪除失敗：{e}")

    # 自動刪除失敗，改為手動
    print(f"    ⚠️  請在你的 Chrome 手動刪除這件商品：")
    print(f"    {url}")
    input("    → 刪除後按 Enter 繼續：")


# ── Phase 3：Bookmarklet 模式（在你自己的 Chrome 執行）──────────────────────
_current_item: dict | None = None
SERVER_PORT = 8765


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/current":
            self._json(_current_item)
        elif path.startswith("/photo/"):
            try:
                idx = int(path.split("/")[-1])
                photos = (_current_item or {}).get("local_photos", [])
                photo_path = Path(photos[idx])
                if photo_path.exists():
                    data = photo_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            except Exception:
                pass
            self.send_response(404)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _make_bookmarklet() -> str:
    js = f"""(function(){{
var B='http://localhost:{SERVER_PORT}';
fetch(B+'/current').then(function(r){{return r.json()}}).then(function(item){{
  if(!item){{alert('No item - check terminal');return;}}
  function fill(el,v){{
    if(!el)return;
    var P=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    var s=Object.getOwnPropertyDescriptor(P,'value');
    if(s&&s.set)s.set.call(el,v);else el.value=v;
    ['input','change'].forEach(function(e){{el.dispatchEvent(new Event(e,{{bubbles:true}}))}});
  }}
  var price=item.price.replace(/[^0-9.]/g,'');
  [['input[name="title"]','input[placeholder*="title" i]',item.title],
   ['textarea[name="description"]','textarea[placeholder*="desc" i]',item.description],
   ['input[name="price"]','input[placeholder*="price" i]',price]
  ].forEach(function(f){{
    var el=document.querySelector(f[0])||document.querySelector(f[1]);
    if(el)fill(el,f[2]);
  }});
  var photos=item.local_photos||[];
  var fi=document.querySelector('input[type="file"]');
  if(fi&&photos.length){{
    Promise.all(photos.map(function(p,i){{
      return fetch(B+'/photo/'+i)
        .then(function(r){{return r.blob()}})
        .then(function(b){{return new File([b],p.split('/').pop()||'photo.jpg',{{type:'image/jpeg'}})}});
    }})).then(function(files){{
      var dt=new DataTransfer();
      files.forEach(function(f){{dt.items.add(f)}});
      Object.defineProperty(fi,'files',{{value:dt.files,configurable:true}});
      fi.dispatchEvent(new Event('change',{{bubbles:true}}));
      fi.dispatchEvent(new Event('input',{{bubbles:true}}));
      console.log('[Vinted] photos set:',files.length);
    }});
  }}
  console.log('[Vinted] filled:',item.title);
}});}})()"""
    return "javascript:" + urllib.parse.quote(js, safe="")


async def phase3_with_nodriver(items: list[dict]) -> None:
    global _current_item

    # 啟動本地 HTTP 伺服器
    server = HTTPServer(("localhost", SERVER_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    # 生成 bookmarklet 並存檔
    bookmarklet = _make_bookmarklet()
    Path("bookmarklet.txt").write_text(bookmarklet)

    print(f"\n{'='*55}")
    print("Phase 3 / 3 — 重新上架（Bookmarklet 模式）")
    print(f"{'='*55}")
    print()
    print("【一次性設定】在 Chrome 新增書籤：")
    print("  1. 按 Ctrl+Shift+B 顯示書籤列")
    print("  2. 在書籤列空白處按右鍵 → 新增頁面")
    print("  3. 名稱：Fill Vinted")
    print("  4. 網址：複製 bookmarklet.txt 的內容貼上")
    print()
    print(f"  檔案位置：{Path('bookmarklet.txt').absolute()}")
    print()
    input("書籤設定完成後按 Enter 開始：")

    print()
    print("流程（每件商品）：")
    print("  1. Chrome 點 Sell now")
    print("  2. 在上架頁點書籤 'Fill Vinted'")
    print("     → 自動填標題、描述、價格、上傳圖片")
    print("  3. 選 Category / Brand / Size / Condition")
    print("  4. 送出 → 回 Terminal 按 Enter\n")

    for i, item in enumerate(items, 1):
        _current_item = item
        photos = [p for p in item.get("local_photos", []) if Path(p).exists()]
        price_num = "".join(c for c in item["price"] if c.isdigit() or c == ".")

        print(f"[{i}/{len(items)}] {item['title']}")
        print(f"  價格：{price_num}  圖片：{len(photos)} 張")
        print(f"  → Chrome 點 Sell now，然後點書籤 'Fill Vinted'")
        input(f"  → 送出後按 Enter 繼續：")

    server.shutdown()
    print("\n✓ 全部完成！")


# ── 主程式 ────────────────────────────────────────────────────────────────────
async def main() -> None:
    # 有備份時讓使用者選擇從哪個 Phase 開始
    backed_up = sorted(BACKUP.glob("*/data.json")) if BACKUP.exists() else []
    if backed_up:
        items = [json.loads(f.read_text()) for f in backed_up]
        print(f"發現現有備份：{len(items)} 件商品")
        print("請選擇要執行的 Phase：")
        print("  1 — 重新備份（Phase 1 → 2 → 3）")
        print("  2 — 跳過備份，從刪除開始（Phase 2 → 3）")
        print("  3 — 直接重新上架（Phase 3，備份已有、商品已刪）")
        choice = input("輸入 1 / 2 / 3：").strip()
    else:
        items = []
        choice = "1"

    # Phase 3 only（不需要 Playwright）
    if choice == "3":
        if not items:
            print("找不到備份資料，請先執行 Phase 1。")
            return
        await phase3_with_nodriver(items)
        return

    # Phase 1 or 2 需要 Playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=150,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()
        await login(page)

        if choice != "2":
            # ── Phase 1
            urls = await collect_urls(page)
            if not urls:
                print("找不到任何商品，程式結束。")
                await browser.close()
                return
            print(f"\n{'='*50}")
            print("Phase 1 / 3 — 備份所有商品資料")
            print(f"{'='*50}")
            items = await phase_backup(page, urls)
            print(f"\n✓ 備份完成，{len(items)} 件商品已存至 {BACKUP}/")

        # ── Phase 2
        print(f"\n{'='*50}")
        print("Phase 2 / 3 — 刪除商品")
        print(f"{'='*50}")
        if confirm("執行 Phase 2？（若已自行刪除請選 n 跳過）"):
            for i, item in enumerate(items, 1):
                print(f"  [{i}/{len(items)}] 刪除：{item['title']}")
                await delete_item(page, item["url"])
                await asyncio.sleep(jitter())
        else:
            print("  跳過 Phase 2（手動刪除）")

        if not confirm("繼續執行 Phase 3（重新上架）？"):
            print("中止。")
            await browser.close()
            return

        await browser.close()

    await phase3_with_nodriver(items)


if __name__ == "__main__":
    asyncio.run(main())
