---

# Rubrik Backup Checker 🔍✨

> Automasi estetis untuk memeriksa status backup database di Rubrik dan
> meng-update Google Sheets berdasarkan tanggal START backup.

---

![status](https://img.shields.io/badge/status-ready-brightgreen)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![playwright](https://img.shields.io/badge/playwright-automation-purple)

---

## ✨ Highlights

- Menentukan kolom tujuan berdasarkan **START TIME** backup, bukan tanggal eksekusi.
- Mendukung berbagai format tanggal dari Rubrik.
- Mode `dry_run` untuk simulasi sebelum update nyata.
- Cache header untuk mengurangi panggilan API Google Sheets.

---

## 🧭 Cara Kerja (Singkat)

1. Ambil daftar DB dari Google Sheets (Kolom B = IP Rubrik, Kolom C = Database Name).
2. Buka browser via Playwright dan login manual ke halaman report Rubrik.
3. Untuk setiap DB: cari di tabel, cocokkan nama + IP, ambil `Start` dan `Status`.
4. Parse `Start` → temukan kolom spreadsheet yang sesuai → tulis hasil (DONE/FAILED/...).

---

## 🛠️ Setup

Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

Siapkan Google Service Account (JSON) dan share spreadsheet ke `client_email`.
Letakkan file credentials di folder project dan update `CONFIG['service_account_file']`.

---

## ⚙️ Konfigurasi (CONFIG)

Edit `CONFIG` di `rubrik_checker.py` untuk menyesuaikan nama spreadsheet, tab, dan mode:

```python
CONFIG = {
        "rubrik_base_url": "https://bankbri.my.rubrik.com",
        "rubrik_report_url": "https://bankbri.my.rubrik.com/reports/299",
        "spreadsheet_name": "Rubrik Backup",
        "sheet_tab": "Backup",
        "service_account_file": "service_account_v2.json",
        "col_db_name_idx": 3,
        "col_ip_rubrik_idx": 2,
        "header_row": 1,
        "data_start_row": 2,
        "dry_run": False,
}
```

---

## 🧩 Logic Detail (Aesthetic Breakdown)

- Search & Retry:

    - Isi search box dengan nama DB.
    - Tunggu 1.2s, jika 0 baris → tunggu 1.5s dan retry 1x.

- Matching:

    - Pastikan nama DB cocok (case-insensitive).
    - Pastikan IP Rubrik muncul di kolom Location.

- Extract & Parse Start:

    - Ambil kolom `Start` (index 7 pada scraped row).
    - Fungsi `parse_start_date()` mencoba banyak format (termasuk `%m/%d/%Y %I:%M:%S %p`).
    - Jika tidak ter-parse → log warning dan fallback ke kolom hari ini.

- Pilih Kolom:

    - `find_column_by_date()` mencari header yang mengandung `day` + `month` (Indonesia/English) atau `MM/DD`.
    - Jika ditemukan → tulis ke kolom tersebut.
    - Jika tidak → fallback ke kolom hari ini.

---

## ✅ Contoh Output (Dry Run)

```
🔍 [ 10] SIKP_KPP_PROSES  (IP: 192.168.53.100)
     ⌨️  Search: 'SIKP_KPP_PROSES'
     🔎 0 baris ditemukan
     🔎 Retry → 1 baris
     Row: ['Rubrik_BRI_Cluster', 'Backup', 'Succeeded', '192.168.53.100\\MSSQLSERVER', 'N/A', 'SIKP_KPP_PROSES', 'SQL Server DB', '06/24/2026 12:00:28 AM', ...]
     ✔ Match! Status='Succeeded' | Location='192.168.53.100\\MSSQLSERVER' | Start='06/24/2026 12:00:28 AM' → 2026-06-24
             → Start 2026-06-24 = hari ini → tulis ke kolom #32
     [DRY RUN] Row 10, Col 32 ← 'DONE BACKUP'
```

---

## 📝 Supported Date Formats

- `%m/%d/%Y %I:%M:%S %p`  (e.g. `06/23/2026 11:04:10 PM`)
- `%m/%d/%Y %I:%M %p`     (e.g. `06/23/2026 11:04 PM`)
- `%d/%m/%Y %H:%M:%S`     (e.g. `23/06/2026 23:00:00`)
- `%Y-%m-%d %H:%M:%S`     (e.g. `2026-06-23 23:00:00`)
- `Jun 23, 2026, 11:00 PM` and other common variations

Tambahkan format lain di `parse_start_date()` jika Rubrik Anda memakai format berbeda.

---

## ⚠️ Troubleshooting Quick Tips

- "Kolom untuk tanggal XXX tidak ditemukan" → periksa header (harus berisi hari + bulan).
- "Format Start tidak dikenali" → lihat log, lalu tambahkan format di `parse_start_date()`.
- "Quota 429" → tambahkan delay `write_delay_sec` di `CONFIG`.

---

## 🎯 Contributing

Jika ingin mempercantik README lebih lanjut (tema gelap, gambar, atau demo gif), kirim PR!

---

Made with ❤️ and Playwright by the Backup Squad

