"""
Rubrik Backup Checker
- Search DB via search box "Search object name and location"
- Cocokkan Location dengan IP Rubrik dari kolom B spreadsheet
- Tulis hasil ke kolom berdasarkan START DATE backup (bukan tanggal script jalan)
- Batch update Google Sheets (hindari quota 429)
"""

import asyncio
import sys
import time
from datetime import datetime, date
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────
# KONFIGURASI
# ─────────────────────────────────────────────
CONFIG = {
    # === RUBRIK ===
    "rubrik_base_url": "https://bank###.my.rubrik.com",
    "rubrik_report_url": "https://bank###.my.rubrik.com/reports/###",

    # === GOOGLE SHEETS ===
    "spreadsheet_name": "########",
    "sheet_tab": "######",
    "service_account_file": "###########.json",

    # === KOLOM SPREADSHEET (1-based) ===
    "col_db_name_idx": 3,       # Kolom C = Database Name
    "col_ip_rubrik_idx": 2,     # Kolom B = IP Rubrik
    "col_backup_status_idx": 8, # Kolom H = Backup Status
    "header_row": 1,
    "data_start_row": 2,

    # === STATUS VALUES ===
    "done_backup_value": "DONE BACKUP",
    "on_progress_value": "ON PROGRESS",
    "failed_value": "FAILED",

    # === ANTI QUOTA (Google Sheets ~60 write/min) ===
    "write_delay_sec": 0,

    "dry_run": False,           # True = tidak update sheet, hanya print
    "debug_limit": None,        # Jumlah DB yang diproses — set None untuk semua
}
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# HELPER: PARSE TANGGAL START DARI RUBRIK
# ─────────────────────────────────────────────
def parse_start_date(start_str: str) -> date | None:
    """
    Parse kolom Start dari tabel Rubrik menjadi objek date.

    Format yang didukung (tambahkan sesuai format aktual di Rubrik kamu):
      - "Jun 23, 2025, 11:00 PM"
      - "Jun 23, 2025 11:00 PM"
      - "2025-06-23 23:00:00"
      - "23/06/2025 23:00"
      - "06/23/2025 23:00"
      - "23 Jun 2025 23:00"

    Tips: jalankan dulu dengan debug_limit=1 dan lihat print row-nya
    untuk tahu format exact yang dipakai Rubrik kamu.
    """
    if not start_str or not start_str.strip():
        return None

    clean = start_str.strip()

    formats = [
        "%b %d, %Y, %I:%M:%S %p", # Jun 23, 2025, 11:00:00 PM
        "%b %d, %Y, %I:%M %p",   # Jun 23, 2025, 11:00 PM
        "%b %d, %Y, %I:%M%p",    # Jun 23, 2025, 11:00PM
        "%b %d, %Y %I:%M:%S %p", # Jun 23, 2025 11:00:00 PM
        "%b %d, %Y %I:%M %p",    # Jun 23, 2025 11:00 PM
        "%b %d, %Y %I:%M%p",     # Jun 23, 2025 11:00PM
        "%b %d, %Y, %H:%M:%S",   # Jun 23, 2025, 23:00:00
        "%b %d, %Y, %H:%M",      # Jun 23, 2025, 23:00
        "%b %d, %Y %H:%M:%S",    # Jun 23, 2025 23:00:00
        "%b %d, %Y %H:%M",       # Jun 23, 2025 23:00
        "%Y-%m-%d %H:%M:%S",     # 2025-06-23 23:00:00
        "%Y-%m-%dT%H:%M:%S",     # 2025-06-23T23:00:00
        "%Y-%m-%d %H:%M",        # 2025-06-23 23:00
        "%d/%m/%Y %H:%M:%S",     # 23/06/2025 23:00:00
        "%d/%m/%Y %H:%M",        # 23/06/2025 23:00
        "%m/%d/%Y %H:%M:%S",     # 06/23/2025 23:00:00
        "%m/%d/%Y %H:%M",        # 06/23/2025 23:00
        "%d/%m/%Y %I:%M:%S %p",  # 23/06/2025 11:00:00 PM
        "%d/%m/%Y %I:%M %p",     # 23/06/2025 11:00 PM
        "%m/%d/%Y %I:%M:%S %p",  # 06/23/2025 11:00:00 PM  ← FORMAT BARU UNTUK LOG MU
        "%m/%d/%Y %I:%M %p",     # 06/23/2025 11:00 PM
        "%d %b %Y %H:%M:%S",     # 23 Jun 2025 23:00:00
        "%d %b %Y %H:%M",        # 23 Jun 2025 23:00
        "%d %b %Y, %H:%M:%S",    # 23 Jun 2025, 23:00:00
        "%d %b %Y, %H:%M",       # 23 Jun 2025, 23:00
    ]

    for fmt in formats:
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            continue

    print(f"   ⚠️  Format Start tidak dikenali: '{start_str}' — pakai kolom hari ini sebagai fallback")
    return None


# ─────────────────────────────────────────────
# WRITE CELL (real-time, retry on 429)
# ─────────────────────────────────────────────
def write_cell(sheet, row: int, col: int, value: str):
    """Tulis langsung ke sheet, retry kalau kena 429."""
    if CONFIG["dry_run"]:
        print(f"   [DRY RUN] Row {row}, Col {col} ← '{value}'")
        return
    for attempt in range(3):
        try:
            sheet.update_cell(row, col, value)
            return
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < 2:
                print(f"   ⏳ Quota 429, tunggu 30s...")
                time.sleep(30)
            else:
                print(f"   ❌ Gagal tulis: {e}")
                return
        except Exception as e:
            print(f"   ❌ Error tulis: {e}")
            return


# ─────────────────────────────────────────────
# LOGIN MANUAL (support OTP)
# ─────────────────────────────────────────────
async def rubrik_login(page: Page) -> bool:
    print(f"🔐 Membuka Rubrik: {CONFIG['rubrik_report_url']}")
    try:
        await page.goto(CONFIG["rubrik_report_url"], wait_until="domcontentloaded", timeout=60000)

        await page.wait_for_timeout(3000)
        sys.stdin.flush()

        print("")
        print("=" * 55)
        print("  🔐 LOGIN MANUAL")
        print("=" * 55)
        print("  1. Isi Email & Password di browser yang terbuka")
        print("  2. Masukkan OTP jika diminta")
        print("  3. Tunggu sampai halaman report /reports/299 muncul")
        print("=" * 55)
        sys.stdin.flush()
        input("  ✅ Tekan ENTER setelah halaman report sudah terbuka... ")
        sys.stdin.flush()

        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except:
            pass

        print("✅ Halaman report siap!\n")
        return True

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


# ─────────────────────────────────────────────
# SEARCH DB DI RUBRIK
# ─────────────────────────────────────────────
async def get_search_input(page: Page):
    """Cari search box tabel Protection Tasks Details."""
    selectors = [
        'input[placeholder="Search object name and location"]',
        'input[placeholder*="Search object"]',
        'input[placeholder*="object name"]',
    ]
    for sel in selectors:
        els = page.locator(sel)
        c = await els.count()
        if c > 0:
            for idx in range(c - 1, -1, -1):
                el = els.nth(idx)
                try:
                    if await el.is_visible():
                        return el
                except:
                    continue
    return None


async def clear_search(page: Page):
    """Bersihkan search box."""
    try:
        inp = await get_search_input(page)
        if inp:
            await inp.fill("")
            await inp.dispatch_event("input")
            await page.wait_for_timeout(200)
    except:
        pass


async def search_db_in_rubrik(page: Page, db_name: str, ip_rubrik: str) -> dict:
    """
    Cari DB via search box di tabel Protection Tasks Details.

    Kolom tabel Rubrik (0-based index):
      0 = Cluster
      1 = Task Type
      2 = Task Status      ← dipakai untuk status
      3 = Location         ← dipakai untuk validasi IP
      4 = Object Name      ← dipakai untuk validasi nama DB
      5 = Object Type
      6 = Start            ← ✅ BARU: dipakai untuk menentukan kolom tanggal
      7 = End
      8 = Duration
      9 = Snapshot
    """
    result = {
        "found": False,
        "status": "",
        "snapshot_type": "",
        "location": "",
        "start_str": "",        # ✅ BARU: raw string Start dari Rubrik
        "start_date": None,     # ✅ BARU: parsed date dari kolom Start
    }

    try:
        inp = await get_search_input(page)
        if inp is None:
            print(f"   ⚠️  Search box tidak ditemukan!")
            return result

        await inp.scroll_into_view_if_needed()
        await inp.click(click_count=3)
        await page.wait_for_timeout(100)
        await page.keyboard.press("Backspace")

        await inp.fill(db_name)
        await inp.dispatch_event("input")
        await inp.dispatch_event("change")

        print(f"   ⌨️  Search: '{db_name}'")

        await page.wait_for_timeout(1200)

        rows_data = await page.evaluate("""() => {
            const result = [];
            const rows = document.querySelectorAll('div[role="row"]');
            rows.forEach(row => {
                const cells = Array.from(row.querySelectorAll('div[role="cell"]'));
                if (cells.length > 0) {
                    result.push(cells.map(cell => cell.innerText.trim()));
                }
            });
            return result;
        }""")

        print(f"   🔎 {len(rows_data)} baris ditemukan")

        if len(rows_data) == 0:
            await page.wait_for_timeout(1500)
            rows_data = await page.evaluate("""() => {
                const result = [];
                const rows = document.querySelectorAll('div[role="row"]');
                rows.forEach(row => {
                    const cells = Array.from(row.querySelectorAll('div[role="cell"]'));
                    if (cells.length > 0) {
                        result.push(cells.map(cell => cell.innerText.trim()));
                    }
                });
                return result;
            }""")
            print(f"   🔎 Retry → {len(rows_data)} baris")

        if len(rows_data) == 0:
            await clear_search(page)
            return result

        ip_clean = ip_rubrik.strip()
        matched_row = None

        for all_cells_text in rows_data:
            print(f"   Row: {all_cells_text}")

            db_found = any(db_name.lower() in txt.lower() for txt in all_cells_text)
            if not db_found:
                continue

            if ip_clean:
                ip_found = any(ip_clean in txt for txt in all_cells_text)
                if not ip_found:
                    print(f"   ↳ DB cocok tapi IP '{ip_clean}' tidak ada di row ini")
                    continue

            # Kolom aktual berdasarkan output terminal:
            # 0=Cluster, 1=TaskType, 2=TaskStatus, 3=Location, 4=N/A,
            # 5=ObjectName, 6=ObjectType, 7=Start, 8=End, 9=Duration, 10=Snapshot
            location = all_cells_text[3] if len(all_cells_text) > 3 else ""
            status   = all_cells_text[2] if len(all_cells_text) > 2 else ""
            matched_row = (location, status, all_cells_text)
            break

        if matched_row is None:
            await clear_search(page)
            return result

        location, status, all_cells_text = matched_row
        result["found"]    = True
        result["location"] = location
        result["status"]   = status

        # Snapshot Type kolom index 10
        if len(all_cells_text) > 10:
            result["snapshot_type"] = all_cells_text[10]

        # ✅ Start time ada di index 7 (bukan 6 — ada kolom N/A ekstra di index 4)
        start_str = all_cells_text[7] if len(all_cells_text) > 7 else ""
        result["start_str"]  = start_str
        result["start_date"] = parse_start_date(start_str)

        if result["start_date"]:
            print(f"   ✔ Match! Status='{status}' | Location='{location}' | Start='{start_str}' → {result['start_date']}")
        else:
            print(f"   ✔ Match! Status='{status}' | Location='{location}' | Start='{start_str}' (tidak terbaca)")

        await clear_search(page)

    except PlaywrightTimeout:
        print(f"   ⏱️  Timeout saat mencari '{db_name}'")
        await clear_search(page)
    except Exception as e:
        print(f"   ⚠️  Error: {e}")
        await clear_search(page)

    return result


# ─────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────
def load_spreadsheet():
    print("📋 Menghubungkan ke Google Sheets...")
    creds_file = Path(CONFIG["service_account_file"])
    if not creds_file.exists():
        raise FileNotFoundError(
            f"File '{creds_file}' tidak ditemukan.\n"
            "Download dari Google Cloud Console dan taruh di folder yang sama."
        )
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(creds_file), scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open(CONFIG["spreadsheet_name"])
    sheet = spreadsheet.worksheet(CONFIG["sheet_tab"])
    print(f"✅ Terhubung ke: '{CONFIG['spreadsheet_name']}' → tab '{CONFIG['sheet_tab']}'")
    return sheet


def find_column_by_date(sheet, target_date: date, headers: list) -> int | None:
    """
    ✅ BARU: Cari kolom yang headernya cocok dengan target_date.
    Menerima parameter headers agar tidak perlu API call berulang.
    Return nilai 1-based index kolom, atau None jika tidak ditemukan.
    """
    bulan_id = {
        1:"Januari", 2:"Februari", 3:"Maret", 4:"April",
        5:"Mei", 6:"Juni", 7:"Juli", 8:"Agustus",
        9:"September", 10:"Oktober", 11:"November", 12:"Desember"
    }
    day_str    = str(target_date.day)
    bulan_full = bulan_id[target_date.month]
    bulan_en   = target_date.strftime("%b")

    for idx, header in enumerate(headers):
        h = str(header).strip()
        if (day_str in h and
                (bulan_full in h or bulan_en in h or
                 target_date.strftime("%m/%d") in h or
                 target_date.strftime("%d/%m") in h)):
            return idx + 1  # 1-based

    print(f"   ⚠️  Kolom untuk tanggal {target_date.strftime('%d %B %Y')} tidak ditemukan di header.")
    return None


def find_today_column(sheet, headers: list) -> int | None:
    """Cari kolom tanggal hari ini dari header row."""
    today = date.today()
    col = find_column_by_date(sheet, today, headers)
    if col:
        print(f"📅 Kolom hari ini ({today.strftime('%d %B %Y')}): kolom #{col}")
    else:
        print(f"⚠️  Kolom untuk {today.strftime('%d %B %Y')} tidak ditemukan.")
        print(f"   10 header pertama: {headers[:10]}")
    return col


def get_db_list(sheet, today_col_idx: int) -> list:
    """Ambil semua DB dari sheet."""
    all_values = sheet.get_all_values()
    db_list = []
    for row_idx, row in enumerate(
        all_values[CONFIG["data_start_row"] - 1:],
        start=CONFIG["data_start_row"]
    ):
        db_name   = row[CONFIG["col_db_name_idx"] - 1].strip()   if len(row) >= CONFIG["col_db_name_idx"]  else ""
        ip_rubrik = row[CONFIG["col_ip_rubrik_idx"] - 1].strip()  if len(row) >= CONFIG["col_ip_rubrik_idx"] else ""
        if not db_name:
            continue
        today_value = row[today_col_idx - 1].strip() if len(row) >= today_col_idx else ""
        db_list.append({
            "row": row_idx,
            "db_name": db_name,
            "ip_rubrik": ip_rubrik,
            "today_col_idx": today_col_idx,
            "today_value": today_value,
        })
    print(f"📊 Total {len(db_list)} database di spreadsheet\n")
    return db_list


# ─────────────────────────────────────────────
# PROSES UTAMA
# ─────────────────────────────────────────────
async def process_all(page: Page, sheet, db_list: list, headers: list):
    stats = {"done": 0, "failed": 0, "not_found": 0, "skipped": 0}

    # ✅ Cache kolom per tanggal — hindari API call berulang ke sheet header
    col_cache: dict[date, int | None] = {}

    for item in db_list:
        db_name   = item["db_name"]
        ip_rubrik = item["ip_rubrik"]
        today_val = item["today_value"].upper()
        today_col = item["today_col_idx"]   # fallback jika start_date tidak terbaca

        # ── SKIP: kolom hari ini sudah DONE BACKUP ──────────────────────────
        # Fitur ini TETAP DIPAKAI dan tidak diubah
        if today_val == CONFIG["done_backup_value"].upper():
            print(f"⏭️  SKIP  [{db_name}] — sudah DONE BACKUP di kolom hari ini")
            stats["skipped"] += 1
            continue
        # ────────────────────────────────────────────────────────────────────

        print(f"\n🔍 [{item['row']:>3}] {db_name}  (IP: {ip_rubrik})")

        res = await search_db_in_rubrik(page, db_name, ip_rubrik)

        if not res["found"]:
            print(f"       → Tidak ditemukan di Rubrik")
            write_cell(sheet, item["row"], today_col, "NOT FOUND")
            stats["not_found"] += 1
            continue

        # ── ✅ VALIDASI TANGGAL: tentukan kolom berdasarkan START DATE ───────
        start_date = res.get("start_date")   # date | None
        target_col = today_col               # default: kolom hari ini (fallback)

        if start_date:
            # Gunakan cache supaya tidak bolak-balik hit API sheets
            if start_date not in col_cache:
                col_cache[start_date] = find_column_by_date(sheet, start_date, headers)

            found_col = col_cache[start_date]

            if found_col:
                target_col = found_col
                if start_date == date.today():
                    print(f"       → Start {start_date} = hari ini → tulis ke kolom #{target_col}")
                else:
                    print(f"       → Start {start_date} ≠ hari ini → tulis ke kolom #{target_col} (tgl {start_date})")
            else:
                print(f"       → Kolom tgl {start_date} tidak ada di sheet, fallback ke kolom hari ini (#{today_col})")
        else:
            print(f"       → Start time tidak terbaca, fallback ke kolom hari ini (#{today_col})")
        # ────────────────────────────────────────────────────────────────────

        # ── Tulis hasil berdasarkan status Rubrik ───────────────────────────
        status     = res["status"].lower()
        loc        = res.get("location", "")
        is_success = "succeeded" in status

        if is_success:
            label = "✅ DONE (with warnings)" if "warning" in status else "✅ DONE"
            print(f"       → {label} | {loc}")
            write_cell(sheet, item["row"], target_col, CONFIG["done_backup_value"])
            stats["done"] += 1

        elif "failed" in status:
            print(f"       → ❌ FAILED | {loc}")
            write_cell(sheet, item["row"], target_col, CONFIG["failed_value"])
            stats["failed"] += 1

        elif "canceled" in status or "cancelled" in status:
            print(f"       → 🚫 CANCELED | {loc}")
            write_cell(sheet, item["row"], target_col, "CANCELED")
            stats["failed"] += 1

        else:
            print(f"       → ❓ Status tidak dikenal: '{res['status']}'")
            write_cell(sheet, item["row"], target_col, res["status"])
            stats["failed"] += 1
        # ────────────────────────────────────────────────────────────────────

    return stats


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    print("=" * 55)
    print("  RUBRIK BACKUP CHECKER")
    print(f"  Tanggal : {date.today().strftime('%d %B %Y')}")
    print(f"  Report  : {CONFIG['rubrik_report_url']}")
    print(f"  Mode    : {'⚠️  DRY RUN' if CONFIG['dry_run'] else '🟢 LIVE UPDATE'}")
    print("=" * 55)

    try:
        sheet = load_spreadsheet()
    except FileNotFoundError as e:
        print(f"\n❌ {e}")
        sys.exit(1)

    # ✅ Ambil headers SEKALI di awal — dipakai oleh find_column_by_date (cache-friendly)
    headers = sheet.row_values(CONFIG["header_row"])
    print(f"📋 Header row diambil ({len(headers)} kolom)")

    today_col_idx = find_today_column(sheet, headers)
    if today_col_idx is None:
        print("❌ Tidak bisa lanjut — kolom hari ini tidak ditemukan.")
        sys.exit(1)

    db_list = get_db_list(sheet, today_col_idx)
    if not db_list:
        print("ℹ️  Tidak ada database yang perlu diproses.")
        return

    if CONFIG.get("debug_limit"):
        db_list = db_list[:CONFIG["debug_limit"]]
        print(f"⚠️  DEBUG MODE: hanya proses {len(db_list)} DB pertama\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=50,
        )
        context = await browser.new_context(
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        try:
            ok = await rubrik_login(page)
            if not ok:
                return

            # ✅ Pass headers ke process_all agar find_column_by_date bisa dipakai
            stats = await process_all(page, sheet, db_list, headers)

            print("\n" + "=" * 55)
            print("  SELESAI")
            print("=" * 55)
            print(f"  ✅ Done Backup  : {stats['done']}")
            print(f"  ❌ Failed/Old   : {stats['failed']}")
            print(f"  🔎 Not Found    : {stats['not_found']}")
            print(f"  ⏭️  Skipped      : {stats['skipped']}")
            print(f"  📊 Total dicek  : {stats['done'] + stats['failed'] + stats['not_found']}")
            print("=" * 55)

        finally:
            await page.wait_for_timeout(3000)
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
