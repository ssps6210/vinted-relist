#!/usr/bin/env python3
"""
vinted_relist.py
自動備份並重新上架 vinted.co.uk 的所有商品。

Phase 1 — 備份：抓取所有商品資料（含 Category/Brand/Size/Condition）+ 下載圖片
Phase 2 — 刪除：逐一刪除原本的商品
Phase 3 — 重新上架：Bookmarklet 自動填入所有欄位 + PyAutoGUI 自動點擊書籤
"""

import asyncio
import json
import random
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from playwright.async_api import async_playwright, Page

# PyAutoGUI（選用）— pip import pyautogui pillow
try:
    import pyautogui
    from PIL import Image
    PYAUTOGUI_OK = True
    pyautogui.PAUSE = 0.4
    pyautogui.FAILSAFE = True
except ImportError:
    PYAUTOGUI_OK = False

# auto_click 模組（座標校準 + 自動點擊下拉選單）
try:
    import auto_click as _ac
    AUTO_CLICK_OK = True
except ImportError:
    AUTO_CLICK_OK = False

# ── 設定 ──────────────────────────────────────────────────────────────────────
with open("config.json", encoding="utf-8") as _f:
    _cfg = json.load(_f)

EMAIL    = _cfg["email"]
PASSWORD = _cfg["password"]
BASE     = "https://www.vinted.co.uk"
BACKUP   = Path("backup")
HEADLESS = False
SERVER_PORT = 8765
BOOKMARK_POS_FILE = Path("bookmark_pos.json")


def jitter(lo=3.0, hi=7.0) -> float:
    return random.uniform(lo, hi)


def confirm(prompt: str) -> bool:
    ans = input(f"\n{prompt} [y/n]: ").strip().lower()
    return ans in ("y", "yes", "")


COOKIE_FILE = Path("cookies.json")


async def save_cookies(page: Page) -> None:
    cookies = await page.context.cookies()
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
    print("  ✓ Cookie 已儲存")


async def load_cookies(page: Page) -> bool:
    if not COOKIE_FILE.exists():
        return False
    cookies = json.loads(COOKIE_FILE.read_text())
    await page.context.add_cookies(cookies)
    return True


# ── 登入 ─────────────────────────────────────────────────────────────────────
async def login(page: Page) -> None:
    print("→ 開啟 Vinted…")
    if await load_cookies(page):
        await page.goto(BASE, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        if not await page.locator('a[href*="signup"], a[href*="login"]').count():
            print("  ✓ 自動登入成功")
            return
        print("  Cookie 已過期，需要重新登入")
    await page.goto(
        "https://www.vinted.co.uk/member/signup/select_type?ref_url=%2F",
        wait_until="domcontentloaded"
    )
    print("  請在瀏覽器手動登入後按 Enter")
    input("  → 登入完成：")
    await page.wait_for_load_state("networkidle")
    await save_cookies(page)
    print("  ✓ 登入成功")


# ── 收集商品 URL ───────────────────────────────────────────────────────────────
async def collect_urls(page: Page) -> list[str]:
    print("→ 請前往你的 Wardrobe 後按 Enter")
    input("  → 按 Enter：")
    prev = 0
    for _ in range(30):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        links = await page.eval_on_selector_all('a[href*="/items/"]', "els => els.map(e => e.href)")
        if len(links) == prev:
            break
        prev = len(links)
    urls = sorted({
        l for l in links
        if l.split("?")[0].rstrip("/").split("/")[-1].isdigit()
    })
    print(f"  找到 {len(urls)} 件商品")
    return urls


# ── 只收集商品 URL（不備份）────────────────────────────────────────────────────
async def collect_urls_only(page: Page) -> list[str]:
    """僅收集 Wardrobe 中未售出/未保留的商品 URL，不抓取文案/不下載圖片/不備份。
    
    會自動比對 backup/ 資料夾，區分：
      - 「已有備份」：backup/ 下有對應資料夾的商品
      - 「新上架」：backup/ 下沒有對應資料夾的商品（從未備份過）
    """
    print("→ 請前往你的 Wardrobe 後按 Enter")
    input("  → 按 Enter：")
    prev = 0
    for _ in range(30):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        links = await page.eval_on_selector_all('a[href*="/items/"]', "els => els.map(e => e.href)")
        if len(links) == prev:
            break
        prev = len(links)

    # 從 Wardrobe 頁面過濾掉已售出 / Reserved 的商品
    sold_reserved_urls = set()
    sold_reserved_urls_raw = await page.evaluate("""() => {
        var results = [];
        var cards = document.querySelectorAll('a[href*="/items/"]');
        cards.forEach(function(card) {
            var allEls = card.querySelectorAll('div, span, p');
            for (var i = 0; i < allEls.length; i++) {
                var el = allEls[i];
                var txt = el.textContent.trim();
                if (el.children.length <= 2 && (txt === 'Sold' || txt === 'Reserved' || txt === 'Réservé' || txt === 'Vendu')) {
                    results.push(card.href);
                    return;
                }
            }
        });
        return results;
    }""")
    sold_reserved_urls = set(sold_reserved_urls_raw)

    urls = sorted({
        l for l in links
        if l.split("?")[0].rstrip("/").split("/")[-1].isdigit()
        and l not in sold_reserved_urls
    })
    skipped = len(sold_reserved_urls)
    print(f"  找到 {len(urls)} 件商品（跳過 {skipped} 件已售出/Reserved）")

    # ── 比對 backup/ 資料夾，區分已有備份 vs 新上架 ─────────────────────────
    existing_ids = {d.name for d in BACKUP.iterdir() if d.is_dir()} if BACKUP.exists() else set()
    backup_items = []   # 已有備份
    new_items = []      # 新上架（從未備份過）

    for url in urls:
        item_id = url.rstrip("/").split("/")[-1]
        if item_id in existing_ids:
            backup_items.append((item_id, url))
        else:
            new_items.append((item_id, url))

    print()
    print(f"  ── Wardrobe 商品分類 ─────────────────────────")
    if backup_items:
        print(f"  已有備份：{len(backup_items)} 件")
        for idx, (item_id, url) in enumerate(backup_items, 1):
            data_file = BACKUP / item_id / "data.json"
            title = item_id
            if data_file.exists():
                try:
                    title = json.loads(data_file.read_text()).get("title", item_id)
                except Exception:
                    pass
            print(f"    {idx}. [{item_id}] {title[:40]}")
    if new_items:
        print(f"  新上架（未備份）：{len(new_items)} 件")
        for idx, (item_id, url) in enumerate(new_items, 1):
            print(f"    {idx}. [{item_id}] {url}")
    print(f"  ──────────────────────────────────────────────")
    print(f"  合計：{len(urls)} 件（已有備份 {len(backup_items)} / 新上架 {len(new_items)}）")
    print()

    return urls


# ── 抓取單一商品（含 Category/Brand/Size/Condition）──────────────────────────
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

    for sel in ['[data-testid="item-status--sold"]', 'text="Sold"', '[class*="sold"]', 'text="Reserved"']:
        if await page.locator(sel).count():
            print("  ↷ 已售出，略過")
            return None

    try:
        title = await page.locator("h1").first.inner_text()

        # 先嘗試展開描述（點擊 Show more 按鈕）
        for expand_sel in [
            '[data-testid="item-description-show-more"]',
            'button:has-text("Show more")',
            'button:has-text("more")',
            '[class*="description"] button',
            'text="and more"',
        ]:
            expand_btn = page.locator(expand_sel)
            if await expand_btn.count():
                try:
                    await expand_btn.first.click()
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
                break

        desc_loc = page.locator('[itemprop="description"]')
        description = await desc_loc.inner_text() if await desc_loc.count() else ""

        # 清理描述文字：移除 Vinted UI 殘留文字（"...and more", "Show less" 等）
        _cleanup_patterns = [
            "\n…and more", "\n...and more", "\nand more",
            "\n…more", "\n...more",
            "\nShow less", "\nShow more",
            "\nAfficher moins", "\nVoir plus",
            "\nVer más", "\nMostrar menos",
            "\nMehr anzeigen", "\nWeniger anzeigen",
            "\nPiù", "\nMostra meno",
        ]
        for _pat in _cleanup_patterns:
            if description.endswith(_pat):
                description = description[:-len(_pat)].rstrip()
        # 也處理 "…and more\n" 在末尾（帶換行）
        description = re.sub(r'\n{0,2}[\u2026\.]{1,3}\s*and more\s*$', '', description).rstrip()
        description = re.sub(r'\n{0,2}Show (?:more|less)\s*$', '', description).rstrip()

        price_loc = page.locator('[data-testid="item-price"]')
        price = await price_loc.inner_text() if await price_loc.count() else ""

        photos: list[str] = await page.eval_on_selector_all(
            '[data-testid="item-photo"] img, .item-photos img, [class*="photo"] img',
            "els => [...new Set(els.map(e => e.src))].filter(s => s.startsWith('http'))"
        )

        # 抓取 Category / Brand / Size / Condition
        attrs = await page.evaluate("""() => {
            var r = {category:'', brand:'', size:'', condition:'', material:''};

            // Category from breadcrumbs
            var bc = [...document.querySelectorAll(
                'nav[aria-label*="breadcrumb" i] a, [class*="breadcrumb" i] a, [data-testid*="breadcrumb"] a'
            )];
            if (bc.length > 1) {
                var parts = bc.slice(1, -1).map(a => a.textContent.trim()).filter(Boolean);
                r.category = parts.join(' > ');
            }

            // Attribute blocks
            var attrSels = [
                '[class*="ItemAttribute"]', '[class*="item-attribute"]',
                '[data-testid*="attribute"]', '[class*="details"] [class*="row"]',
                '[class*="details-list"] li'
            ];
            attrSels.forEach(function(sel) {
                document.querySelectorAll(sel).forEach(function(el) {
                    var txt = el.textContent.trim().toLowerCase();
                    var val = el.querySelector(
                        '[class*="value"], [class*="body"], h3, strong, p:last-child, span:last-child'
                    );
                    var valTxt = val ? val.textContent.trim() : '';
                    if (!valTxt) return;
                    if (!r.brand && txt.includes('brand')) r.brand = valTxt;
                    if (!r.size && txt.includes('size')) r.size = valTxt;
                    if (!r.condition && txt.includes('condition')) r.condition = valTxt;
                    if (!r.material && txt.includes('material')) r.material = valTxt;
                });
            });

            // Fallback: dt/dd pairs
            if (!r.brand || !r.size || !r.condition || !r.material) {
                document.querySelectorAll('dt').forEach(function(dt) {
                    var key = dt.textContent.trim().toLowerCase().replace(':', '');
                    var dd = dt.nextElementSibling;
                    var v = dd ? dd.textContent.trim() : '';
                    if (!r.brand && key === 'brand') r.brand = v;
                    if (!r.size && key === 'size') r.size = v;
                    if (!r.condition && (key === 'condition' || key === 'item condition')) r.condition = v;
                    if (!r.material && key === 'material') r.material = v;
                });
            }

            return r;
        }""")

        return {
            "url": url,
            "title": title.strip(),
            "description": description.strip(),
            "price": price.strip(),
            "photos": photos,
            "category":  attrs.get("category", ""),
            "brand":     attrs.get("brand", ""),
            "size":      attrs.get("size", ""),
            "condition": attrs.get("condition", ""),
            "material":  attrs.get("material", ""),
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
        brand_info = f"Brand:{data.get('brand','?')}" if data.get('brand') else ""
        cat_info   = data.get('category', '')[:25] or "no-cat"
        print(f"✓  ({len(local_photos)} 張圖  {brand_info}  {cat_info})")
        await asyncio.sleep(jitter())
    return items


# ── Phase 2：刪除 ─────────────────────────────────────────────────────────────
async def delete_item(page: Page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(jitter(2, 4))

        # 檢查頁面是否為 404（商品已不存在）
        title = await page.title()
        page_url = page.url
        if "404" in title or "/404" in page_url or "could not be found" in title.lower():
            print("    ✓ 商品已不存在（404），自動跳過")
            return

        # 檢查是否已售出 / Reserved — 已售出的商品無法刪除，跳過
        is_sold = await page.evaluate("""() => {
            // 遍歷所有元素，找純文字的 "Sold" / "Reserved" badge
            var allEls = document.querySelectorAll('div, span, p');
            for (var i = 0; i < allEls.length; i++) {
                var el = allEls[i];
                var txt = el.textContent.trim().toLowerCase();
                // 只檢查葉節點或幾乎無子元素的元素
                if (el.children.length <= 2) {
                    if (txt === 'sold' || txt === 'reserved' || txt === 'item sold' || txt === 'sold out'
                        || txt === 'vendu' || txt === 'réservé') {
                        return txt;
                    }
                }
            }
            return '';
        }""")
        if is_sold:
            print(f"    ✓ 已售出/Reserved（{is_sold}），跳過不刪除")
            return

        for sel in ['[data-testid="item-more-options"]', 'button[aria-label*="more" i]',
                    'button[aria-label*="options" i]', '[data-testid="item-actions"] button']:
            btn = page.locator(sel)
            if await btn.count():
                await btn.first.click()
                await asyncio.sleep(1.5)
                break
        for sel in ['[data-testid="delete-item"]', 'button:has-text("Delete")', 'a:has-text("Delete")']:
            btn = page.locator(sel)
            if await btn.count():
                await btn.first.click()
                await asyncio.sleep(1.5)
                break
        await asyncio.sleep(2)
        for sel in ['button:has-text("Confirm")', '[data-testid="confirm-delete"]',
                    '[role="dialog"] button:has-text("Delete")', '[role="dialog"] button:has-text("Confirm")']:
            btn = page.locator(sel)
            if await btn.count():
                await btn.first.click()
                await asyncio.sleep(3)
                print("    ✓ 已刪除")
                return
    except Exception as e:
        print(f"    ✗ 自動刪除失敗：{e}")
    print(f"    ⚠️  請手動刪除：{url}")
    input("    → 刪除後按 Enter：")


# ── HTTP Server ───────────────────────────────────────────────────────────────
_current_item: dict | None = None


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


# ── Bookmarklet（全欄位：Title/Desc/Price/Photos/Brand/Size/Condition/Category）
def _make_bookmarklet() -> str:
    js = f"""(async function(){{
var B='http://localhost:{SERVER_PORT}';
var item=await fetch(B+'/current').then(r=>r.json()).catch(()=>null);
if(!item){{alert('無法連接伺服器，請確認 Terminal 正在執行');return;}}
function sleep(ms){{return new Promise(r=>setTimeout(r,ms));}}
function fill(el,v){{
  if(!el)return;
  var P=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
  var s=Object.getOwnPropertyDescriptor(P,'value');
  if(s&&s.set)s.set.call(el,v);else el.value=v;
  ['input','change'].forEach(function(e){{el.dispatchEvent(new Event(e,{{bubbles:true}}))}});
}}
function matchText(els,text){{
  var t=text.trim().toLowerCase();
  return [...els].find(function(e){{return e.textContent.trim().toLowerCase()===t;}})
    ||[...els].find(function(e){{return e.textContent.trim().toLowerCase().includes(t);}});
}}

// ── 基本欄位 ──────────────────────────────────────────────────────────
var price=String(parseInt(item.price.replace(/[^0-9]/g,''),10)||0);
[['input[name="title"]','input[placeholder*="title" i]',item.title],
 ['textarea[name="description"]','textarea[placeholder*="desc" i]',item.description],
 ['input[name="price"]','input[placeholder*="price" i]',price]
].forEach(function(f){{
  var el=document.querySelector(f[0])||document.querySelector(f[1]);
  if(el)fill(el,f[2]);
}});

// ── 圖片上傳 ────────────────────────────────────────────────────────────
var photos=item.local_photos||[];
var fi=document.querySelector('input[type="file"]');
if(fi&&photos.length){{
  var files=await Promise.all(photos.map(function(p,i){{
    return fetch(B+'/photo/'+i).then(r=>r.blob())
      .then(b=>new File([b],p.split('/').pop()||'photo.jpg',{{type:'image/jpeg'}}));
  }}));
  var dt=new DataTransfer();
  files.forEach(f=>dt.items.add(f));
  Object.defineProperty(fi,'files',{{value:dt.files,configurable:true}});
  fi.dispatchEvent(new Event('change',{{bubbles:true}}));
  fi.dispatchEvent(new Event('input',{{bubbles:true}}));
  await sleep(1200);
}}

// ── Brand ────────────────────────────────────────────────────────────────
if(item.brand){{
  var bIn=document.querySelector(
    'input[name*="brand" i],input[id*="brand" i],input[placeholder*="brand" i],input[aria-label*="brand" i]'
  );
  if(bIn){{
    bIn.focus();fill(bIn,item.brand);
    await sleep(1200);
    var opts=[...document.querySelectorAll(
      '[role="option"],[class*="suggestion"] li,[class*="autocomplete"] li,[class*="dropdown"] li,[class*="Dropdown"] li'
    )];
    var m=opts.find(o=>o.textContent.trim().toLowerCase()===item.brand.toLowerCase())
         ||opts.find(o=>o.textContent.trim().toLowerCase().startsWith(item.brand.split(' ')[0].toLowerCase()));
    if(m)m.click();else if(opts.length)opts[0].click();
    await sleep(400);
  }}
}}

// ── Material ──────────────────────────────────────────────────────────────
if(item.material){{
  await sleep(200);
  var mIn=document.querySelector(
    'input[name*="material" i],input[id*="material" i],input[placeholder*="material" i],input[aria-label*="material" i]'
  );
  if(mIn){{
    mIn.focus();fill(mIn,item.material);
    await sleep(1200);
    var mOpts=[...document.querySelectorAll(
      '[role="option"],[class*="suggestion"] li,[class*="autocomplete"] li,[class*="dropdown"] li,[class*="Dropdown"] li'
    )];
    var mm=mOpts.find(o=>o.textContent.trim().toLowerCase()===item.material.toLowerCase())
          ||mOpts.find(o=>o.textContent.trim().toLowerCase().includes(item.material.toLowerCase()));
    if(mm)mm.click();else if(mOpts.length)mOpts[0].click();
    await sleep(400);
  }}
}}

// ── Condition ────────────────────────────────────────────────────────────
if(item.condition){{
  await sleep(200);
  var cEls=document.querySelectorAll(
    '[data-testid*="condition"] button,[class*="condition"] button,[class*="condition"] label,[class*="Condition"] button'
  );
  var cMatch=matchText(cEls,item.condition);
  if(cMatch){{cMatch.click();await sleep(300);}}
}}

// ── Size ─────────────────────────────────────────────────────────────────
if(item.size){{
  await sleep(200);
  var sEls=document.querySelectorAll(
    '[data-testid*="size"] button,[class*="size"] button,[class*="size"] label,[class*="Size"] button'
  );
  var sMatch=matchText(sEls,item.size);
  if(sMatch){{sMatch.click();await sleep(300);}}
}}

// ── Category（逐層點擊樹狀選單）─────────────────────────────────────────
if(item.category){{
  await sleep(300);
  var catPath=item.category.split('>').map(s=>s.trim()).filter(Boolean);
  var catBtn=document.querySelector(
    '[data-testid*="category"] button,[class*="CategorySelect"] button,[class*="category-select"] button'
  );
  if(!catBtn)catBtn=[...document.querySelectorAll('button,[role="button"]')]
    .find(b=>/select.*(category|what are you)/i.test(b.textContent));
  if(catBtn){{
    catBtn.click();
    for(var lvl of catPath){{
      await sleep(900);
      var catItems=[...document.querySelectorAll(
        '[role="option"],[role="menuitem"],[class*="CategoryItem"] button,[class*="category-item"] button,li button,li a,[class*="cell"] button'
      )];
      var t=catItems.find(e=>e.textContent.trim()===lvl)
           ||catItems.find(e=>e.textContent.trim().toLowerCase().includes(lvl.toLowerCase()));
      if(t)t.click();
      else console.warn('[Vinted] category level not found:',lvl);
    }}
    await sleep(600);
  }} else {{
    console.warn('[Vinted] category button not found');
  }}
}}

console.log('[Vinted] ✓ 填寫完成:',item.title);
}})()"""
    return "javascript:" + urllib.parse.quote(js, safe="")


# ── PyAutoGUI 輔助（自動點擊書籤列）─────────────────────────────────────────
def _activate_chrome() -> None:
    subprocess.run(
        ['osascript', '-e', 'tell application "Google Chrome" to activate'],
        capture_output=True
    )
    time.sleep(0.6)


def _track_mouse_position(label: str, warmup: float = 2.0, sample_duration: float = 2.0) -> tuple[int, int]:
    """追蹤滑鼠位置，回傳最穩定的座標（中位數）。
    
    warmup: 用戶切換到 Chrome 的緩衝時間
    sample_duration: 實際取樣時間
    """
    print(f"  ⏱️  {int(warmup)} 秒後開始追蹤滑鼠…")
    print(f"  請立即切換到 Chrome，將滑鼠移到「{label}」上方且不要動")
    time.sleep(warmup)
    _activate_chrome()

    print(f"  正在追蹤「{label}」滑鼠（{sample_duration} 秒）…")
    samples = []
    for _ in range(int(sample_duration / 0.15)):
        pos = pyautogui.position()
        samples.append((pos.x, pos.y))
        time.sleep(0.15)

    # 用中位數取代 Counter（抗滑鼠抖動）
    xs = sorted(s[0] for s in samples)
    ys = sorted(s[1] for s in samples)
    mid = len(xs) // 2
    median_x = xs[mid]
    median_y = ys[mid]
    return (median_x, median_y)


def _setup_positions() -> dict:
    """一次性設定：Sell now + 書籤的螢幕座標。"""
    if BOOKMARK_POS_FILE.exists():
        data = json.loads(BOOKMARK_POS_FILE.read_text())
        required = ["sell_now", "bookmark"]
        if all(k in data for k in required):
            return data

    print()
    print("  ── 座標設定（只需做一次）─────────────────────────")
    print("  請先在 Chrome 打開 Vinted 首頁（已登入狀態）")
    input("  → 準備好後按 Enter：")

    data = {}
    if BOOKMARK_POS_FILE.exists():
        data = json.loads(BOOKMARK_POS_FILE.read_text())

    # Sell now 按鈕
    if "sell_now" not in data:
        print()
        print("  ── Sell now 按鈕位置 ──")
        x, y = _track_mouse_position("Sell now 按鈕")
        data["sell_now"] = {"x": x, "y": y}
        print(f"  ✓ Sell now 位置已儲存：({x}, {y})")
        time.sleep(0.5)

    # 書籤
    if "bookmark" not in data:
        print()
        print("  ── 'Fill Vinted' 書籤位置 ──")
        print("  1. 確認 Chrome 已顯示書籤列（Cmd+Shift+B）")
        print("  2. 確認 'Fill Vinted' 書籤已存在於書籤列")
        x, y = _track_mouse_position("Fill Vinted 書籤")
        data["bookmark"] = {"x": x, "y": y}
        print(f"  ✓ 書籤位置已儲存：({x}, {y})")

    BOOKMARK_POS_FILE.write_text(json.dumps(data, indent=2))
    print()
    print(f"  ✓ 所有位置已儲存到 {BOOKMARK_POS_FILE}")
    return data


def _click_sell_now(positions: dict) -> bool:
    """用 PyAutoGUI 自動點擊 Sell now 按鈕。"""
    if not PYAUTOGUI_OK or "sell_now" not in positions:
        return False
    try:
        pos = positions["sell_now"]
        _activate_chrome()
        time.sleep(0.4)
        pyautogui.click(pos["x"], pos["y"])
        return True
    except Exception as e:
        print(f"    Sell now 點擊失敗：{e}")
        return False


def _click_fill_vinted_bookmark(positions: dict) -> bool:
    """用 PyAutoGUI 點擊 'Fill Vinted' 書籤。"""
    if not PYAUTOGUI_OK or "bookmark" not in positions:
        return False
    try:
        pos = positions["bookmark"]
        _activate_chrome()
        time.sleep(0.4)
        pyautogui.click(pos["x"], pos["y"])
        return True
    except Exception as e:
        print(f"    書籤點擊失敗：{e}")
        return False


# ── 載入 category_config ──────────────────────────────────────────────────────
CATEGORY_CONFIG_FILE = Path("category_config.json")


def _load_category_config() -> dict:
    if CATEGORY_CONFIG_FILE.exists():
        return json.loads(CATEGORY_CONFIG_FILE.read_text())
    return {"defaults": {}, "items": {}}


def _get_item_cat_config(item: dict, cat_cfg: dict) -> dict:
    """根據 item ID 取得其 Category/Brand/Condition，找不到就用 defaults。"""
    item_id = item.get("url", "").rstrip("/").split("/")[-1]
    return cat_cfg.get("items", {}).get(item_id, cat_cfg.get("defaults", {}))


# ── Phase 3 主流程（整合 Bookmarklet + Auto-Click 下拉選單）─────────────────
async def phase3_with_nodriver(items: list[dict]) -> None:
    global _current_item

    # 啟動本地 HTTP 伺服器
    server = HTTPServer(("localhost", SERVER_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    # 生成 bookmarklet
    bookmarklet = _make_bookmarklet()
    Path("bookmarklet.txt").write_text(bookmarklet)

    print(f"\n{'='*58}")
    print("Phase 3 / 3 — 重新上架")
    print(f"{'='*58}")
    print()
    print("【Bookmarklet 已就緒】")
    print("  ✓ Chrome 工具列已有 'Fill Vinted' 書籤")
    print()
    print("  書籤會自動填入：")
    print("  ✓ 標題、描述、價格、圖片")
    print()

    # ── PyAutoGUI + Auto-Click 設定 ──────────────────────────────────────────
    auto_click_sell_now = False      # 自動點擊 Sell now
    auto_click_bookmark = False      # 自動點擊書籤
    auto_click_dropdowns = False     # 自動點擊下拉選單（Category/Brand/Condition）
    positions = {}
    dropdown_coords = None

    if PYAUTOGUI_OK:
        print("  ✓ PyAutoGUI 已載入")
        if confirm("  啟用 PyAutoGUI 自動化？（推薦）"):
            # Sell now + 書籤位置
            if confirm("  啟用自動點擊 Sell now + 書籤？（省去每次手動點）"):
                positions = _setup_positions()
                auto_click_sell_now = "sell_now" in positions
                auto_click_bookmark = "bookmark" in positions
                if auto_click_sell_now:
                    print(f"  ✓ Sell now 已啟用（{positions['sell_now']}）")
                if auto_click_bookmark:
                    print(f"  ✓ 書籤已啟用（{positions['bookmark']}）")
                if not auto_click_sell_now and not auto_click_bookmark:
                    print("  跳過自動點擊")

            # Auto-Click 下拉選單
            if AUTO_CLICK_OK:
                if confirm("  啟用自動點擊下拉選單（Category/Brand/Condition）？"):
                    # 載入或校準座標
                    dropdown_coords = _ac.load_coords()
                    if not dropdown_coords:
                        print("  首次使用，需要校準下拉選單座標…")
                        dropdown_coords = _ac.calibrate_interactive()
                    else:
                        print(f"  ✓ 已載入下拉選單座標（{len(dropdown_coords)} 個位置）")
                        if confirm("  是否重新校準座標？（如果頁面佈局有變動請選 y）"):
                            dropdown_coords = _ac.calibrate_interactive()
                    auto_click_dropdowns = dropdown_coords is not None and len(dropdown_coords) >= 4
                    if auto_click_dropdowns:
                        print("  ✓ 自動點擊下拉選單已啟用")
                    else:
                        print("  ⚠️  座標不足，跳過自動下拉選單")
            else:
                print()
                print("  ⚠️  無法載入 auto_click 模組，跳過自動下拉選單")
    else:
        print()
        print("  提示：安裝 PyAutoGUI 可啟用自動化功能：")
        print("  pip install pyautogui pillow")

    # ── 載入 category_config ──────────────────────────────────────────────────
    cat_cfg = _load_category_config()
    if auto_click_dropdowns:
        print(f"  ✓ 已載入 category_config.json（{len(cat_cfg.get('items', {}))} 件商品配置）")

    print()
    input("設定完成後按 Enter 開始上架：")
    print()

    # 顯示操作說明
    print("每件商品流程：")
    if auto_click_sell_now and auto_click_bookmark and auto_click_dropdowns:
        print("  1. 回 Terminal 按 Enter → 自動點 Sell now + 書籤填寫")
        print("  2. 等待數秒 → 自動選擇 Category / Brand / Condition")
        print("  3. 檢查無誤後手動送出，按 Enter 繼續下一件")
    elif auto_click_sell_now and auto_click_bookmark:
        print("  1. 回 Terminal 按 Enter → 自動點 Sell now + 書籤填寫")
        print("  2. 手動選擇 Category / Brand / Condition 下拉選單")
        print("  3. 檢查無誤後手動送出，按 Enter 繼續下一件")
    elif auto_click_bookmark:
        print("  1. Chrome 點 'Sell now' 進入上架頁")
        print("  2. 回 Terminal 按 Enter → 自動觸發書籤填寫文字/圖片")
        print("  3. 手動選擇 Category / Brand / Condition 下拉選單")
        print("  4. 檢查無誤後手動送出，按 Enter 繼續下一件")
    else:
        print("  1. Chrome 點 'Sell now' 進入上架頁")
        print("  2. 點書籤 'Fill Vinted' → 自動填寫文字/圖片")
        print("  3. 手動選擇 Category / Brand / Condition 下拉選單")
        print("  4. 檢查無誤後手動送出，按 Enter 繼續下一件")
    print()

    for i, item in enumerate(items, 1):
        _current_item = item
        photos = [p for p in item.get("local_photos", []) if Path(p).exists()]
        price_num = "".join(c for c in item["price"] if c.isdigit() or c == ".")

        # 從 category_config 取得此商品的分類資訊
        ic = _get_item_cat_config(item, cat_cfg)
        cfg_category = ic.get("category", "")
        cfg_subcategory = ic.get("subcategory", "")
        cfg_brand = ic.get("brand", "")

        print(f"[{i}/{len(items)}] {item['title']}")
        print(f"  Price: {price_num}  Photos: {len(photos)}")
        if cfg_brand:     print(f"  Brand:       {cfg_brand}")
        if cfg_category:  print(f"  Category:    {cfg_category}" + (f" > {cfg_subcategory}" if cfg_subcategory else ""))

        # ── Step 1: 自動點 Sell now + 觸發 Bookmarklet ────────────────────
        if auto_click_sell_now:
            input("  → 按 Enter 自動點 Sell now + 書籤：")
            # 點 Sell now
            ok = _click_sell_now(positions)
            if ok:
                print("  ✓ 已點 Sell now，等待頁面載入…")
                time.sleep(3)  # 等上架頁面載入
            else:
                print("  ⚠️  Sell now 點擊失敗，請手動點 Sell now")
                input("  → 點完後按 Enter：")

            # 點書籤
            if auto_click_bookmark:
                time.sleep(1)
                ok = _click_fill_vinted_bookmark(positions)
                if ok:
                    print("  ✓ 書籤已觸發，等待 JS 填寫…（約 5 秒）")
                    time.sleep(5)
                else:
                    print("  ⚠️  書籤點擊失敗，請手動點 'Fill Vinted'")
                    input("  → 點完後按 Enter：")
        elif auto_click_bookmark:
            input("  → 進入 Sell now 頁後按 Enter（自動觸發書籤）：")
            time.sleep(1.5)
            ok = _click_fill_vinted_bookmark(positions)
            if ok:
                print("  ✓ 書籤已觸發，等待 JS 填寫…（約 5 秒）")
                time.sleep(5)
            else:
                print("  ⚠️  自動點擊失敗，請手動點書籤 'Fill Vinted'")
                input("  → 點完後按 Enter：")
        else:
            print("  → Chrome 點 Sell now → 點書籤 'Fill Vinted'")
            input("  → 填寫完成後按 Enter：")

        # ── Step 2: 自動選擇 Category / Brand / Condition 下拉選單 ──────────
        if auto_click_dropdowns and dropdown_coords:
            _ac.human_delay(1.0, 2.0)  # 等 bookmarklet 完成

            # Category
            if cfg_category and "category_input" in dropdown_coords:
                cat_text = cfg_subcategory if cfg_subcategory else cfg_category
                print(f"  → 自動選擇 Category: {cat_text}…")
                try:
                    _ac.select_search_dropdown(
                        dropdown_coords["category_input"],
                        dropdown_coords["category_first"],
                        cat_text
                    )
                    print(f"    ✓ Category 已選擇")
                except Exception as e:
                    print(f"    ✗ Category 選擇失敗：{e}")
            _ac.human_delay(1.0, 2.0)

            # Brand
            if cfg_brand and "brand_input" in dropdown_coords:
                print(f"  → 自動選擇 Brand: {cfg_brand}…")
                try:
                    _ac.select_search_dropdown(
                        dropdown_coords["brand_input"],
                        dropdown_coords["brand_first"],
                        cfg_brand
                    )
                    print(f"    ✓ Brand 已選擇")
                except Exception as e:
                    print(f"    ✗ Brand 選擇失敗：{e}")
            _ac.human_delay(1.0, 2.0)

            # Condition
            if "condition_input" in dropdown_coords:
                cond = ic.get("condition", "New with tags")
                print(f"  → 自動選擇 Condition: {cond}…")
                try:
                    _ac.select_simple_dropdown(
                        dropdown_coords["condition_input"],
                        dropdown_coords["condition_nwt"]
                    )
                    print(f"    ✓ Condition 已選擇")
                except Exception as e:
                    print(f"    ✗ Condition 選擇失敗：{e}")
            _ac.human_delay(1.0, 2.0)

            # Material（材質）
            cfg_material = ic.get("material", "")
            if cfg_material and "material_input" in dropdown_coords:
                print(f"  → 自動選擇 Material: {cfg_material}…")
                try:
                    _ac.select_search_dropdown(
                        dropdown_coords["material_input"],
                        dropdown_coords["material_first"],
                        cfg_material
                    )
                    print(f"    ✓ Material 已選擇")
                except Exception as e:
                    print(f"    ✗ Material 選擇失敗：{e}")

            print(f"  ✓ 自動化完成，請檢查後送出")
        else:
            print(f"  → 手動選擇 Category / Brand / Condition / Material")
            input("  → 選擇完成後按 Enter：")

        print(f"  ✓ [{i}/{len(items)}] 完成\n")

    server.shutdown()
    print("✓ 全部完成！")


# ── 主程式 ────────────────────────────────────────────────────────────────────
def _show_backup_list(items: list[dict]) -> None:
    """顯示備份商品清單。"""
    print(f"\n  ── 已備份商品（共 {len(items)} 件）──────────────")
    for idx, item in enumerate(items, 1):
        title = item.get("title", "?")[:40]
        price = item.get("price", "")
        brand = item.get("brand", "")
        item_id = item.get("url", "").rstrip("/").split("/")[-1]
        line = f"    {idx:2d}. [{item_id}] {title}"
        if brand:
            line += f"  ({brand})"
        if price:
            line += f"  {price}"
        print(line)
    print(f"  {'─'*50}")


def _select_backup_items(items: list[dict]) -> list[dict]:
    """讓使用者選擇要上架的商品。回傳選中的 items 列表。"""
    print(f"\n  共 {len(items)} 件已備份商品")
    print("  輸入方式：")
    print("    • 數字：1,3,5（逗號分隔）")
    print("    • 範圍：1-5")
    print("    • 混合：1,3-6,8")
    print("    • 全部：Enter（不輸入）")
    sel = input("  請選擇要上架的商品：").strip()

    if not sel:
        return items

    indices = set()
    for part in sel.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                for n in range(int(a), int(b) + 1):
                    indices.add(n)
            except ValueError:
                pass
        else:
            try:
                indices.add(int(part))
            except ValueError:
                pass

    if not indices:
        print("  無效輸入，使用全部商品")
        return items

    selected = [items[i - 1] for i in sorted(indices) if 1 <= i <= len(items)]
    print(f"  已選擇 {len(selected)} 件商品")
    return selected


async def main() -> None:
    backed_up = sorted(BACKUP.glob("*/data.json")) if BACKUP.exists() else []
    if backed_up:
        all_items = [json.loads(f.read_text()) for f in backed_up]
        print(f"發現現有備份：{len(all_items)} 件商品")
        _show_backup_list(all_items)
        print()
        print("請選擇：")
        print("  1 — 重新備份（Phase 1 → 2 → 3）")
        print("  2 — 跳過備份，從刪除開始（Phase 2 → 3）")
        print("  3 — 直接重新上架（Phase 3，選擇商品）")
        choice = input("輸入 1 / 2 / 3：").strip()
    else:
        all_items = []
        choice = "1"

    if choice == "3":
        if not all_items:
            print("找不到備份，請先執行 Phase 1。")
            return
        items = _select_backup_items(all_items)
        await phase3_with_nodriver(items)
        return

    # ── 選項 2 的子選項 ──────────────────────────────────────────────────────
    delete_mode = None  # "backup" 或 "wardrobe"
    if choice == "2":
        print("請選擇刪除方式：")
        print("  A — 使用備份資料刪除（從 backup 讀取商品 URL）")
        print("  B — 直接從 Wardrobe 刪除所有商品（收集最新 URL）")
        sub = input("輸入 A / B：").strip().lower()
        if sub == "b":
            delete_mode = "wardrobe"
        else:
            delete_mode = "backup"

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

        if choice == "1":
            # Phase 1：備份
            urls = await collect_urls(page)
            if not urls:
                print("找不到任何商品。")
                await browser.close()
                return
            print(f"\n{'='*50}")
            print("Phase 1 / 3 — 備份所有商品資料")
            print(f"{'='*50}")
            items = await phase_backup(page, urls)
            print(f"\n✓ 備份完成：{len(items)} 件 → {BACKUP}/")

        elif choice == "2" and delete_mode == "wardrobe":
            # 從 Wardrobe 收集最新 URL 並刪除
            urls = await collect_urls_only(page)
            if not urls:
                print("找不到任何商品。")
                await browser.close()
                return
            # 用 URL 建立臨時 items 列表（僅含 url + title）
            items = [{"url": u, "title": u.rstrip("/").split("/")[-1]} for u in urls]

        # choice == "2" + delete_mode == "backup"：使用現有 items（從 backup 讀取）
        if choice == "2" and delete_mode == "backup":
            items = all_items

        print(f"\n{'='*50}")
        print("Phase 2 / 3 — 刪除商品")
        print(f"{'='*50}")
        if confirm(f"要刪除 {len(items)} 件商品嗎？"):
            for i, item in enumerate(items, 1):
                print(f"  [{i}/{len(items)}] 刪除：{item.get('title', item['url'])}")
                await delete_item(page, item["url"])
                await asyncio.sleep(jitter())
        else:
            print("  跳過 Phase 2")

        if not confirm("繼續 Phase 3（重新上架）？"):
            print("中止。")
            await browser.close()
            return
        await browser.close()

    await phase3_with_nodriver(items)


if __name__ == "__main__":
    asyncio.run(main())
