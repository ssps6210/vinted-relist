# 🛍️ Vinted Auto-Relist

Automatically back up, delete, and re-list all your Vinted items in one go — pushing everything back to the top of search results without paying for bumps.

> Built for [vinted.co.uk](https://www.vinted.co.uk) · Works on Mac & Windows

---

## ✨ Why This Exists

Vinted's search ranks newer listings higher. Re-listing your items is the free way to get visibility again — but doing it manually for 20+ items is tedious. This tool automates the whole process.

---

## 🔄 How It Works (3 Phases)

```
Phase 1 — Backup    Scrape all item data (title, description, price, photos)
                    → saved to backup/ folder locally

Phase 2 — Delete    Automatically delete each item from Vinted one by one

Phase 3 — Re-list   Fill in new listings automatically via a browser bookmarklet
                    (you only need to pick Category / Brand / Size / Condition)
```

Each phase pauses and asks for confirmation before continuing — so you can stop and resume at any point.

---

## 🚀 Setup

### 1. Clone & install

```bash
git clone https://github.com/ssps6210/vinted-relist.git
cd vinted-relist

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` with your Vinted login credentials:

```json
{
  "email": "your-email@example.com",
  "password": "your-password"
}
```

> ⚠️ `config.json` is in `.gitignore` — it will never be committed.

---

## ▶️ Run

```bash
python3 vinted_relist.py
```

The script will open a Chrome window and guide you through each phase interactively.

### Phase 1 — Backup
- Navigates to your Wardrobe page
- Scrapes all active listings (skips sold/reserved items automatically)
- Downloads photos and saves everything to `backup/`

### Phase 2 — Delete
- Deletes each listing one by one
- Falls back to manual deletion with a prompt if automation fails

### Phase 3 — Re-list (Bookmarklet)
- Starts a local server on `localhost:8765`
- Generates a **bookmarklet** saved to `bookmarklet.txt`
- You add the bookmarklet to Chrome once, then click it on each new listing page to auto-fill title, description, price, and photos
- You only need to manually select: **Category**, **Brand**, **Size**, **Condition**

---

## 📂 File Structure

```
vinted-relist/
├── vinted_relist.py       Main script
├── config.json            Your credentials (NOT committed — create from example)
├── config.example.json    Template for config.json
├── requirements.txt       Python dependencies
├── backup/                Item data + photos (auto-created, NOT committed)
├── bookmarklet.txt        Generated bookmarklet (auto-created, NOT committed)
└── cookies.json           Session cookies (auto-created, NOT committed)
```

---

## ⚙️ Requirements

- Python 3.10+
- Google Chrome (for Phase 3 bookmarklet)
- A Vinted account with active listings

---

## ⚠️ Notes

- This tool uses [Playwright](https://playwright.dev/python/) to control a real browser — it mimics normal user behaviour, but use it responsibly.
- Vinted may update its page structure over time; selectors in the script may need updating if things break.
- **Sold items are automatically skipped** during backup — they won't be deleted or re-listed.

---

## ☕ Support

If this saved you time, consider buying me a coffee!

<a href="https://www.buymeacoffee.com/ssps6210noa" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height:60px;width:217px;" />
</a>

---

## Tags

`#vinted` `#automation` `#python` `#playwright` `#reselling` `#relist` `#productivity`
