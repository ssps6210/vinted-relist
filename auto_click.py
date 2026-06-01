#!/usr/bin/env python3
"""
auto_click.py — Vinted Phase 3 模擬人類滑鼠點擊

Bookmarklet 填完 title/desc/price/photos 之後，
這個腳本用 pyautogui 模擬真實滑鼠移動 + 點擊，
自動選擇 Category / Brand / Condition 等下拉選單。

所有操作：
  - 滑鼠移動帶貝塞爾曲線軌跡（不是直線）
  - 每次點擊有隨機偏移（±5px）
  - 隨機延遲（0.3-2.5秒）
  - 打字間隔隨機（30-120ms）

依賴：pip install pyautogui pyobjc-framework-Quartz
"""

import json
import math
import random
import sys
import time
from pathlib import Path

try:
    import pyautogui
except ImportError:
    print("✗ 安裝依賴：pip install pyautogui pyobjc-framework-Quartz")
    sys.exit(1)

# ── 安全設定 ──────────────────────────────────────────────────────────────────
pyautogui.FAILSAFE = True   # 滑鼠移到螢幕左上角 = 緊急停止
pyautogui.PAUSE = 0.05

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "category_config.json"
BACKUP_DIR = BASE_DIR / "backup"
COORDS_FILE = BASE_DIR / "coords.json"


# ══════════════════════════════════════════════════════════════════════════════
# 人類模擬核心
# ══════════════════════════════════════════════════════════════════════════════

def _bezier_points(start: tuple, end: tuple, steps: int = 20) -> list[tuple]:
    """
    三階貝塞爾曲線 — 產生自然的滑鼠移動軌跡
    不是直線移動，而是微微弧形，像真人手動
    """
    x0, y0 = start
    x3, y3 = end
    
    # 隨機控制點（在起終點之間的隨機偏移）
    dist = math.hypot(x3 - x0, y3 - y0)
    wobble = min(dist * 0.3, 80)  # 偏移量跟距離成正比，最大 80px
    
    x1 = x0 + (x3 - x0) * 0.3 + random.uniform(-wobble, wobble)
    y1 = y0 + (y3 - y0) * 0.3 + random.uniform(-wobble, wobble)
    x2 = x0 + (x3 - x0) * 0.7 + random.uniform(-wobble, wobble)
    y2 = y0 + (y3 - y0) * 0.7 + random.uniform(-wobble, wobble)
    
    points = []
    for i in range(steps + 1):
        t = i / steps
        # 三階貝塞爾公式
        x = (1-t)**3 * x0 + 3*(1-t)**2*t * x1 + 3*(1-t)*t**2 * x2 + t**3 * x3
        y = (1-t)**3 * y0 + 3*(1-t)**2*t * y1 + 3*(1-t)*t**2 * y2 + t**3 * y3
        points.append((int(x), int(y)))
    
    return points


def human_move(x: int, y: int) -> None:
    """人類式滑鼠移動：貝塞爾曲線 + 隨機速度"""
    current = pyautogui.position()
    target = (x + random.randint(-4, 4), y + random.randint(-4, 4))
    
    steps = random.randint(15, 30)
    points = _bezier_points(current, target, steps)
    
    for px, py in points:
        pyautogui.moveTo(px, py, _pause=False)
        # 每步之間微小隨機延遲（模擬手速不穩）
        time.sleep(random.uniform(0.008, 0.025))


def human_click(x: int, y: int, button: str = "left") -> None:
    """人類式點擊：移動到位 → 短暫停頓 → 按下 → 隨機釋放"""
    human_move(x, y)
    
    # 點擊前短暫猶豫（0.05-0.2秒）
    time.sleep(random.uniform(0.05, 0.20))
    
    # 按下
    pyautogui.mouseDown(button=button)
    
    # 按住時間（50-150ms，模擬真實按壓）
    time.sleep(random.uniform(0.05, 0.15))
    
    # 釋放
    pyautogui.mouseUp(button=button)
    
    # 點擊後短暫停頓
    time.sleep(random.uniform(0.1, 0.3))


def human_double_click(x: int, y: int) -> None:
    """人類式雙擊"""
    human_click(x, y)
    time.sleep(random.uniform(0.05, 0.12))
    human_click(x + random.randint(-2, 2), y + random.randint(-2, 2))


def human_type(text: str, base_interval: float = 0.05) -> None:
    """人類式打字：每個字元之間隨機間隔"""
    for char in text:
        if char == " ":
            pyautogui.press("space")
        elif char == "\n":
            pyautogui.press("enter")
        elif char.isascii():
            pyautogui.press(char)
        else:
            # 非 ASCII 字元（中文等）用剪貼簿
            import subprocess
            subprocess.run(["osascript", "-e",
                f'set the clipboard to "{char}"'],
                capture_output=True)
            pyautogui.hotkey("command", "v")
        
        # 隨機間隔（±50%）
        delay = base_interval * random.uniform(0.5, 1.5)
        # 偶爾長停頓（模擬思考）
        if random.random() < 0.05:
            delay += random.uniform(0.3, 0.8)
        time.sleep(delay)


def human_type_fast(text: str) -> None:
    """快速打字（用於搜尋框，但仍帶隨機間隔）"""
    import subprocess
    # 用剪貼簿一次貼上，比逐字打更快更可靠
    subprocess.run(["osascript", "-e",
        f'set the clipboard to "{text}"'],
        capture_output=True)
    time.sleep(random.uniform(0.1, 0.2))
    pyautogui.hotkey("command", "v")
    time.sleep(random.uniform(0.3, 0.6))


def human_scroll(clicks: int = 3) -> None:
    """人類式滾動"""
    for _ in range(clicks):
        pyautogui.scroll(-1)
        time.sleep(random.uniform(0.1, 0.3))


def human_delay(lo: float = 0.5, hi: float = 1.5) -> None:
    """隨機等待"""
    time.sleep(random.uniform(lo, hi))


def long_delay(lo: float = 2.0, hi: float = 4.0) -> None:
    """較長等待（頁面載入）"""
    time.sleep(random.uniform(lo, hi))


# ══════════════════════════════════════════════════════════════════════════════
# 座標管理
# ══════════════════════════════════════════════════════════════════════════════

def load_coords() -> dict:
    """載入已儲存的座標"""
    if COORDS_FILE.exists():
        return json.loads(COORDS_FILE.read_text())
    return {}


def save_coords(coords: dict) -> None:
    """儲存座標"""
    COORDS_FILE.write_text(json.dumps(coords, indent=2))


def calibrate_interactive() -> dict:
    """
    互動式校準：讓用戶把滑鼠移到指定位置，腳本記錄座標
    只需做一次，之後自動使用
    """
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║  🎯 座標校準 — 只需做一次                          ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    print("請在 Chrome 打開 Vinted 的 Sell now 頁面")
    print("（不需要填任何東西，空白頁面就好）")
    input("準備好後按 Enter...")
    print()
    
    coords = {}
    
    targets = [
        ("category_input",    "Category 搜尋框"),
        ("category_first",    "Category 下拉第一個選項的位置（大概在搜尋框下方 50px 處）"),
        ("brand_input",       "Brand 搜尋框"),
        ("brand_first",       "Brand 下拉第一個選項的位置"),
        ("condition_input",   "Condition 下拉觸發按鈕"),
        ("condition_nwt",     "'New with tags' 選項的位置"),
    ]
    
    for key, desc in targets:
        print(f"  → 把滑鼠移到【{desc}】上方")
        print(f"     然後不要動滑鼠！")
        for countdown in [3, 2, 1]:
            print(f"     {countdown}...", end="\r", flush=True)
            time.sleep(1)
        x, y = pyautogui.position()
        coords[key] = {"x": x, "y": y}
        print(f"     ✓ 記錄：({x}, {y})     ")
        time.sleep(0.5)
    
    save_coords(coords)
    print()
    print(f"✓ 座標已儲存到 {COORDS_FILE}")
    print("  下次執行不需要再校準")
    print()
    
    return coords


# ══════════════════════════════════════════════════════════════════════════════
# Vinted 下拉選單操作
# ══════════════════════════════════════════════════════════════════════════════

def select_search_dropdown(
    trigger_coords: dict,
    first_option_coords: dict,
    search_text: str,
    wait: float = 1.5
) -> bool:
    """
    Vinted 搜尋式下拉選單操作流程：
    1. 點擊觸發區域（打開下拉）
    2. 等待搜尋框出現
    3. 輸入搜尋文字
    4. 等待結果載入
    5. 點擊第一個選項
    """
    tx, ty = trigger_coords["x"], trigger_coords["y"]
    ox, oy = first_option_coords["x"], first_option_coords["y"]
    
    # Step 1: 點擊觸發
    human_click(tx, ty)
    human_delay(0.5, 1.0)
    
    # Step 2: 清空並輸入搜尋文字
    pyautogui.hotkey("command", "a")
    human_delay(0.1, 0.2)
    human_type_fast(search_text)
    
    # Step 3: 等待下拉結果
    human_delay(wait, wait + 1.0)
    
    # Step 4: 點擊第一個選項
    human_click(ox, oy)
    human_delay(0.3, 0.6)
    
    return True


def select_simple_dropdown(
    trigger_coords: dict,
    option_coords: dict,
) -> bool:
    """
    Vinted 簡單下拉選單（如 Condition）：
    1. 點擊觸發按鈕
    2. 等待下拉出現
    3. 點擊目標選項
    """
    tx, ty = trigger_coords["x"], trigger_coords["y"]
    ox, oy = option_coords["x"], option_coords["y"]
    
    # 點擊觸發
    human_click(tx, ty)
    human_delay(0.8, 1.5)
    
    # 點擊選項
    human_click(ox, oy)
    human_delay(0.3, 0.6)
    
    return True


# ══════════════════════════════════════════════════════════════════════════════
# 資料載入
# ══════════════════════════════════════════════════════════════════════════════

def load_category_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"defaults": {}, "items": {}}


def get_all_items() -> list[dict]:
    items = []
    for item_dir in sorted(BACKUP_DIR.iterdir()):
        if not item_dir.is_dir():
            continue
        data_file = item_dir / "data.json"
        if data_file.exists():
            items.append(json.loads(data_file.read_text()))
    return items


def get_item_category(item: dict, cat_config: dict) -> dict:
    item_id = item.get("url", "").rstrip("/").split("/")[-1]
    return cat_config.get("items", {}).get(item_id, cat_config.get("defaults", {}))


# ══════════════════════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║  Vinted Auto-Click — Phase 3                        ║")
    print("║  真實滑鼠模擬 + 貝塞爾曲線軌跡 + 隨機延遲          ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    print("⚠️  安全：滑鼠移到螢幕左上角 = 緊急停止")
    print()
    
    # 載入設定
    cat_config = load_category_config()
    items = get_all_items()
    
    if not items:
        print("✗ 找不到備份資料，請先執行 vinted_relist.py Phase 1")
        return
    
    print(f"找到 {len(items)} 件商品")
    
    # 載入或校準座標
    coords = load_coords()
    if not coords:
        print("首次使用，需要校準座標...")
        coords = calibrate_interactive()
    else:
        print(f"✓ 已載入座標（{len(coords)} 個位置）")
        print("  如果 Vinted 頁面有變動，請刪除 coords.json 重新校準")
    
    print()
    print("流程（每件商品）：")
    print("  1. Chrome 開好 Sell now 頁面")
    print("  2. 點 bookmarklet → 自動填文字/圖片")
    print("  3. 回到這裡按 Enter → 腳本模擬滑鼠選下拉選單")
    print("  4. 你檢查後 Submit")
    print()
    
    input("準備好後按 Enter 開始...")
    
    success = 0
    fail = 0
    
    for i, item in enumerate(items, 1):
        title = item.get("title", "?")
        cat = get_item_category(item, cat_config)
        category = cat.get("category", "Accessories")
        subcategory = cat.get("subcategory", "")
        brand = cat.get("brand", "National Museum of History")
        
        print(f"\n{'='*55}")
        print(f"[{i}/{len(items)}] {title}")
        print(f"  Category: {category}" + (f" > {subcategory}" if subcategory else ""))
        print(f"  Brand: {brand}")
        print(f"  Condition: New with tags")
        print(f"{'='*55}")
        
        input("  → Bookmarklet 填完後，按 Enter 開始自動點擊...")
        
        # 倒數（讓用戶切到 Chrome）
        print("  ⏳ 3秒後開始...", end="", flush=True)
        for s in [3, 2, 1]:
            time.sleep(1)
            print(f" {s}", end="", flush=True)
        print(" ▶")
        
        try:
            # ── 選 Category ──
            print(f"  → 正在選 Category: {category}...")
            if "category_input" in coords:
                select_search_dropdown(
                    coords["category_input"],
                    coords["category_first"],
                    category
                )
                print(f"    ✓ Category 選好了")
            else:
                print(f"    ⚠️  缺少 category 座標")
            
            human_delay(1.0, 2.0)
            
            # ── 選 Brand ──
            print(f"  → 正在選 Brand: {brand}...")
            if "brand_input" in coords:
                select_search_dropdown(
                    coords["brand_input"],
                    coords["brand_first"],
                    brand
                )
                print(f"    ✓ Brand 選好了")
            else:
                print(f"    ⚠️  缺少 brand 座標")
            
            human_delay(1.0, 2.0)
            
            # ── 選 Condition ──
            print(f"  → 正在選 Condition: New with tags...")
            if "condition_input" in coords:
                select_simple_dropdown(
                    coords["condition_input"],
                    coords["condition_nwt"]
                )
                print(f"    ✓ Condition 選好了")
            else:
                print(f"    ⚠️  缺少 condition 座標")
            
            print(f"\n  ✓ 全部完成！請檢查後 Submit")
            success += 1
            
        except pyautogui.FailSafeException:
            print("\n  ⏹  緊急停止（滑鼠移到了左上角）")
            break
        except KeyboardInterrupt:
            print("\n  ⏹  Ctrl+C 中斷")
            break
        except Exception as e:
            print(f"  ✗ 錯誤：{e}")
            fail += 1
        
        input("  → Submit 後按 Enter 繼續下一件...")
    
    print(f"\n{'='*55}")
    print(f"✓ 完成！成功：{success}，失敗：{fail}")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
