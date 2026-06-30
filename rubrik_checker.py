"""
Rubrik Backup Checker
- Search DB via search box "Search object name and location"
- Cocokkan Location dengan IP Rubrik dari kolom B spreadsheet
- Tulis hasil ke kolom berdasarkan START DATE backup (bukan tanggal script jalan)
- Skip otomatis SEBELUM search Rubrik jika kolom sudah DONE BACKUP
- Batch update Google Sheets (hindari quota 429)

─────────────────────────────────────────────────────────────
 SETUP
─────────────────────────────────────────────────────────────
 Isi nilai di bagian CONFIG di bawah (rubrik_base_url,
 rubrik_report_url, spreadsheet_name, service_account_file)
 sesuai environment kamu sendiri sebelum dijalankan.

─────────────────────────────────────────────────────────────
 CARA SETTING SKIP
─────────────────────────────────────────────────────────────
 "skip_lookback_days": 0  → hanya cek kolom HARI INI
 "skip_lookback_days": 1  → cek kolom hari ini + KEMARIN (umum)
 "skip_lookback_days": 2  → cek hari ini + kemarin + 2 hari lalu

 Catatan: Skip dilakukan SEBELUM buka Rubrik, jadi makin banyak
 DB yang bisa di-skip, makin cepat script selesai.
─────────────────────────────────────────────────────────────
"""

import asyncio
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from requests.exceptions import ConnectionError

import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────
# KONFIGURASI
# ⚠️  Ganti nilai di bawah sesuai environment kamu sendiri
# sebelum menjalankan / mem-publish script ini.
# ─────────────────────────────────────────────
CONFIG = {
    # === RUBRIK ===
    "rubrik_base_url": "https://your-org.my.rubrik.com",
    "rubrik_report_url": "https://your-org.my.rubrik.com/reports/REPORT_ID",

    # === GOOGLE SHEETS ===
    "spreadsheet_name": "Backup Tracker",
    "sheet_tab": "Backup",
    "service_account_file": "service_account.json",

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

    # === SKIP CONFIG ===
    "skip_lookback_days": 0,    # 0 = cek hari ini saja, 1 = + kemarin, dst
    "skip_target_date": "",     # isi "YYYY-MM-DD" untuk cek tanggal spesifik

    "dry_run": False,           # True = tidak update sheet, hanya print
    "debug_limit": None,        # jumlah DB yang diproses, None = semua
}
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# HELPER: PARSE TANGGAL START DARI RUBRIK
# ─────────────────────────────────────────────
def parse_start_date(start_str: str) -> date | None:
    """
    Parse kolom Start dari tabel Rubrik menjadi objek date.
    MM/DD/YYYY diutamakan karena format Rubrik menggunakan gaya Amerika.
    """
    if not start_str or not start_str.strip():
        return None

    clean = start_str.strip()

    formats = [
        "%b %d, %Y, %I:%M:%S %p",  # Jun 23, 2025, 11:00:00 PM
        "%b %d, %Y, %I:%M %p",     # Jun 23, 2025, 11:00 PM
        "%b %d, %Y, %I:%M%p",      # Jun 23, 2025, 11:00PM
        "%b %d, %Y %I:%M:%S %p",   # Jun 23, 2025 11:00:00 PM
        "%b %d, %Y %I:%M %p",      # Jun 23, 2025 11:00 PM
        "%b %d, %Y %I:%M%p",       # Jun 23, 2025 11:00PM
        "%b %d, %Y, %H:%M:%S",     # Jun 23, 2025, 23:00:00
        "%b %d, %Y, %H:%M",        # Jun 23, 2025, 23:00
        "%b %d, %Y %H:%M:%S",      # Jun 23, 2025 23:00:00
        "%b %d, %Y %H:%M",         # Jun 23, 2025 23:00
        "%Y-%m-%d %H:%M:%S",       # 2025-06-23 23:00:00
        "%Y-%m-%dT%H:%M:%S",       # 2025-06-23T23:00:00
        "%Y-%m-%d %H:%M",          # 2025-06-23 23:00
        # ✅ MM/DD DULU (format Rubrik / Amerika) — HARUS di atas DD/MM
        "%m/%d/%Y %H:%M:%S",       # 06/23/2025 23:00:00
        "%m/%d/%Y %H:%M",          # 06/23/2025 23:00
        "%m/%d/%Y %I:%M:%S %p",    # 06/23/2025 11:00:00 PM
        "%m/%d/%Y %I:%M %p",       # 06/23/2025 11:00 PM
        # DD/MM sebagai fallback terakhir
        "%d/%m/%Y %H:%M:%S",       # 23/06/2025 23:00:00
        "%d/%m/%Y %H:%M",          # 23/06/2025 23:00
        "%d/%m/%Y %I:%M:%S %p",    # 23/06/2025 11:00:00 PM
        "%d/%m/%Y %I:%M %p",       # 23/06/2025 11:00 PM
        "%d %b %Y %H:%M:%S",       # 23 Jun 2025 23:00:00
        "%d %b %Y %H:%M",          # 23 Jun 2025 23:00
        "%d %b %Y, %H:%M:%S",      # 23 Jun 2025, 23:00:00
        "%d %b %Y, %H:%M",         # 23 Jun 2025, 23:00
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
        print("  3. Tunggu sampai halaman report muncul")
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
    result = {
        "found": False,
        "status": "",
        "snapshot_type": "",
        "location": "",
        "start_str": "",
        "start_date": None,
    }

    try:
        inp = await get_search_input(page)
        if inp is None:
            print(f"    ⚠️  Search box tidak ditemukan!")
            return result

        await inp.scroll_into_view_if_needed()
        await inp.click(click_count=3)
        await page.wait_for_timeout(100)
        await page.keyboard.press("Backspace")

        db_name_clean = db_name.strip()
        ip_clean = ip_rubrik.strip()

        await inp.fill(db_name_clean)
        await inp.dispatch_event("input")
        await inp.dispatch_event("change")
        await page.keyboard.press("Enter")

        print(f"    ⌨️  Search: '{db_name_clean}'")

        # Tunggu sampai ada baris di DOM — max 3 detik, cek tiap 1.5 detik
        rows_data = []
        for attempt in range(2):
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
            if len(rows_data) > 0:
                print(f"    🔎 {len(rows_data)} baris ditemukan (attempt {attempt+1})")
                break
            print(f"    ⏳ Tunggu halaman load... ({attempt+1}/2)")

        if len(rows_data) == 0:
            print(f"    ⚠️  Timeout — 0 baris setelah 3 detik")
            await clear_search(page)
            return result

        matched_row = None

        for all_cells_text in rows_data:
            # Cek Object Name (index 5) secara eksplisit — hindari false positive
            if len(all_cells_text) <= 5:
                continue
            db_found = db_name_clean.lower() == all_cells_text[5].lower()
            if not db_found:
                continue

            # Cek Location (index 3) secara eksplisit — hindari false positive
            if ip_clean:
                if len(all_cells_text) <= 3:
                    continue
                location_val = all_cells_text[3]
                ip_found = ip_clean in location_val or "AG" in location_val.upper()
                if not ip_found:
                    continue

            if len(all_cells_text) >= 7:
                matched_row = all_cells_text
                break

        if matched_row is None:
            await clear_search(page)
            return result

        result["found"]    = True
        result["location"] = matched_row[3] if len(matched_row) > 3 else ""
        result["status"]   = matched_row[2] if len(matched_row) > 2 else ""

        if len(matched_row) > 10:
            result["snapshot_type"] = matched_row[10]

        start_str = matched_row[7] if len(matched_row) > 7 else ""
        result["start_str"]  = start_str
        result["start_date"] = parse_start_date(start_str)

        if result["start_date"]:
            print(f"    ✔ Match! Status='{result['status']}' | Location='{result['location']}' | Start='{start_str}' → {result['start_date']}")
        else:
            print(f"    ✔ Match! Status='{result['status']}' | Location='{result['location']}' | Start='{start_str}' (tidak terbaca)")

        await clear_search(page)

    except PlaywrightTimeout:
        print(f"    ⏱️  Timeout saat mencari '{db_name}'")
        await clear_search(page)
    except Exception as e:
        print(f"    ⚠️  Error internal saat search: {e}")
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
            "Download dari Google Cloud Console dan taruh di folder yang sama "
            "(jangan di-commit ke git!)."
        )
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(creds_file), scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open(CONFIG["spreadsheet_name"])
    sheet = spreadsheet.worksheet(CONFIG["sheet_tab"])
    print(f"✅ Terhubung ke spreadsheet → tab '{CONFIG['sheet_tab']}'")
    return sheet


def find_column_by_date(target_date: date, headers: list) -> int | None:
    """
    Mencari kolom yang headernya cocok dengan target_date secara presisi.
    Mendukung format Indonesia (Juni) dan Inggris (Jun).
    """
    bulan_id = {
        1: "januari",  2: "februari", 3: "maret",     4: "april",
        5: "mei",      6: "juni",     7: "juli",      8: "agustus",
        9: "september",10: "oktober", 11: "november",  12: "desember"
    }

    day_str    = str(target_date.day)
    bulan_full = bulan_id[target_date.month]
    bulan_en   = target_date.strftime("%b").lower()
    tahun_str  = str(target_date.year)

    pola_id_full  = f"{day_str} {bulan_full} {tahun_str}"
    pola_en_short = f"{day_str} {bulan_en} {tahun_str}"

    for idx, header in enumerate(headers):
        h = str(header).strip().lower()

        if pola_id_full in h or pola_en_short in h:
            return idx + 1

        # Fallback format numerik
        if day_str in h and tahun_str in h:
            if (target_date.strftime("%m/%d") in h or
                target_date.strftime("%d/%m") in h or
                target_date.strftime("%Y-%m-%d") in h):
                return idx + 1

    return None


def find_today_column(headers: list) -> int | None:
    """Cari kolom tanggal hari ini dari header row."""
    today = date.today()
    col = find_column_by_date(today, headers)
    if col:
        print(f"📅 Kolom hari ini ({today.strftime('%d %B %Y')}): kolom #{col}")
    else:
        print(f"⚠️  Kolom untuk {today.strftime('%d %B %Y')} tidak ditemukan.")
        print(f"   10 header pertama: {headers[:10]}")
    return col


def get_db_list(sheet) -> list:
    """Ambil semua DB dari sheet beserta seluruh data baris."""
    all_values = sheet.get_all_values()
    db_list = []
    for row_idx, row in enumerate(
        all_values[CONFIG["data_start_row"] - 1:],
        start=CONFIG["data_start_row"]
    ):
        db_name   = row[CONFIG["col_db_name_idx"] - 1].strip()  if len(row) >= CONFIG["col_db_name_idx"]  else ""
        ip_rubrik = row[CONFIG["col_ip_rubrik_idx"] - 1].strip() if len(row) >= CONFIG["col_ip_rubrik_idx"] else ""
        if not db_name:
            continue
        db_list.append({
            "row":      row_idx,
            "db_name":  db_name,
            "ip_rubrik": ip_rubrik,
            "row_data": row,    # seluruh data baris untuk cek skip
        })
    print(f"📊 Total {len(db_list)} database di spreadsheet\n")
    return db_list


def get_cell_value(row_data: list, col_idx: int) -> str:
    """Ambil nilai cell dari row_data secara aman (hindari IndexError)."""
    if col_idx - 1 < len(row_data):
        return str(row_data[col_idx - 1]).strip().upper()
    return ""


def build_skip_cols(headers: list) -> list[tuple[int, date, str]]:
    """
    Bangun daftar kolom yang akan dicek untuk skip.
    - Kalau skip_target_date diisi → hanya cek tanggal itu saja
    - Kalau kosong → pakai skip_lookback_days dari hari ini
    """
    target_str = CONFIG.get("skip_target_date", "").strip()
    result     = []

    if target_str:
        target_date = datetime.strptime(target_str, "%Y-%m-%d").date()
        col_idx = find_column_by_date(target_date, headers)
        if col_idx:
            result.append((col_idx, target_date, target_date.strftime("%d %b %Y")))
    else:
        lookback = CONFIG.get("skip_lookback_days", 1)
        today    = date.today()
        for delta in range(lookback + 1):
            check_date = today - timedelta(days=delta)
            col_idx    = find_column_by_date(check_date, headers)
            if col_idx:
                label = "hari ini" if delta == 0 else check_date.strftime("%d %b %Y")
                result.append((col_idx, check_date, label))

    return result


# ─────────────────────────────────────────────
# PROSES UTAMA
# ─────────────────────────────────────────────
async def process_all(page: Page, sheet, db_list: list, headers: list, today_col: int):
    stats = {"done": 0, "failed": 0, "not_found": 0, "skipped": 0}

    col_cache: dict[date, int | None] = {}

    skip_cols = build_skip_cols(headers)
    skip_labels = [label for _, _, label in skip_cols]
    print(f"🔍 Kolom yang dicek untuk skip: {', '.join(skip_labels)}\n")

    for item in db_list:
        db_name   = item["db_name"]
        ip_rubrik = item["ip_rubrik"]
        row_data  = item["row_data"]

        # ── SKIP: cek SEBELUM buka Rubrik ───────────────────────────────────
        skipped = False
        for col_idx, check_date, label in skip_cols:
            if get_cell_value(row_data, col_idx) == "DONE BACKUP":
                print(f"⏭️  SKIP  [{db_name}] — kolom {label} (#{col_idx}) sudah DONE BACKUP")
                stats["skipped"] += 1
                skipped = True
                break
        if skipped:
            continue
        # ────────────────────────────────────────────────────────────────────

        print(f"\n🔍 [{item['row']:>4}] {db_name}  (IP: {ip_rubrik})")

        res = await search_db_in_rubrik(page, db_name, ip_rubrik)

        if not res["found"]:
            print(f"       → Tidak ditemukan sama sekali di Rubrik")
            write_cell(sheet, item["row"], today_col, "NOT FOUND")
            stats["not_found"] += 1
            continue

        start_date = res.get("start_date")
        target_col = None

        if start_date:
            if start_date not in col_cache:
                col_cache[start_date] = find_column_by_date(start_date, headers)
            target_col = col_cache[start_date]

        if not target_col:
            print(f"       → Kolom tgl {start_date} tidak ada di sheet, fallback ke kolom hari ini")
            target_col = today_col

        status     = res["status"].lower()
        loc        = res.get("location", "")
        is_success = "succeeded" in status

        if is_success:
            label       = "✅ DONE (with warnings)" if "warning" in status else "✅ DONE"
            final_value = "DONE BACKUP"
            stats["done"] += 1
        elif "failed" in status:
            label       = "❌ FAILED"
            final_value = CONFIG["failed_value"]
            stats["failed"] += 1
        elif "canceled" in status or "cancelled" in status:
            label       = "🚫 CANCELED"
            final_value = "CANCELED"
            stats["failed"] += 1
        else:
            label       = f"❓ UNKNOWN ({res['status']})"
            final_value = res["status"]
            stats["failed"] += 1

        if start_date == date.today():
            print(f"       → {label} | {loc} | Start {start_date} = Hari Ini → Tulis ke kolom #{target_col}")
        else:
            print(f"       → {label} | {loc} | Start {start_date} = Kemarin/Lalu → Tulis ke kolom #{target_col} | Kolom hari ini dibiarkan kosong")

        write_cell(sheet, item["row"], target_col, final_value)

        while len(item["row_data"]) < target_col:
            item["row_data"].append("")
        item["row_data"][target_col - 1] = final_value

    return stats


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    target_str = CONFIG.get("skip_target_date", "").strip()
    if target_str:
        skip_desc = f"tanggal spesifik: {target_str}"
    else:
        lookback = CONFIG.get("skip_lookback_days", 1)
        skip_desc = "hari ini saja" if lookback == 0 else f"hari ini + {lookback} hari ke belakang"

    print("=" * 55)
    print("  RUBRIK BACKUP CHECKER")
    print(f"  Tanggal : {date.today().strftime('%d %B %Y')}")
    print(f"  Mode    : {'⚠️  DRY RUN' if CONFIG['dry_run'] else '🟢 LIVE UPDATE'}")
    print(f"  Skip    : cek {skip_desc}")
    print("=" * 55)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            sheet = load_spreadsheet()
            break
        except ConnectionError as e:
            if attempt < max_retries - 1:
                print(f"⚠️ Koneksi terputus. Mencoba ulang dalam 3 detik... ({attempt + 1}/{max_retries})")
                time.sleep(3)
            else:
                print("❌ Gagal terhubung ke Google Sheets setelah beberapa kali mencoba.")
                raise e

    headers = sheet.row_values(CONFIG["header_row"])
    print(f"📋 Header row diambil ({len(headers)} kolom)")

    target_str = CONFIG.get("skip_target_date", "").strip()
    if target_str:
        target_date = datetime.strptime(target_str, "%Y-%m-%d").date()
        today_col_idx = find_column_by_date(target_date, headers)
        if today_col_idx:
            print(f"📅 Kolom target ({target_str}): kolom #{today_col_idx}")
        else:
            today_col_idx = find_today_column(headers)
    else:
        today_col_idx = find_today_column(headers)

    if today_col_idx is None:
        print("❌ Tidak bisa lanjut — kolom hari ini tidak ditemukan.")
        sys.exit(1)

    db_list = get_db_list(sheet)
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

            stats = await process_all(page, sheet, db_list, headers, today_col_idx)

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
