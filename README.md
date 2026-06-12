# Rubrik Backup Checker 🔍

Script otomatis untuk cek status backup database di Rubrik dan update Google Sheets.

---

## Cara Kerja

1. Baca list DB dari Google Sheets tab "Backup Daily"
2. Login ke Rubrik via browser (Playwright)
3. Cari setiap DB di halaman Reports → Protection Tasks Details
4. Jika sudah ter-backup hari ini → update cell jadi **DONE BACKUP**
5. Jika belum / gagal → update cell sesuai statusnya

---

## Setup (Lakukan Sekali)

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Buat Google Service Account

1. Buka https://console.cloud.google.com
2. Buat project baru (atau pakai yang ada)
3. Enable **Google Sheets API** dan **Google Drive API**
4. Buat **Service Account** → buat key → download sebagai `service_account.json`
5. Taruh file `service_account.json` di folder yang sama dengan script ini
6. **Penting:** Share spreadsheet kamu ke email service account (ada di file json, field `client_email`)
   dengan akses **Editor**

### 3. Edit CONFIG di `rubrik_checker.py`

Buka file `rubrik_checker.py` dan sesuaikan bagian CONFIG:

```python
CONFIG = {
    "rubrik_url": "https://xxx.xxx.xx.xx",   # URL Rubrik kamu
    "rubrik_username": "admin",                 # Username Rubrik
    "rubrik_password": "password123",           # Password Rubrik
    "spreadsheet_name": "Backup Daily",
    "sheet_tab": "Backup",
    "service_account_file": "service_account.json",
    ...
}
```

---

## Cara Menjalankan

```bash
python rubrik_checker.py
```

Browser Chromium akan terbuka otomatis, login ke Rubrik, lalu mulai cek satu per satu.

### Mode Test (Dry Run)
Tidak akan mengubah spreadsheet, hanya print hasilnya:

```python
# Di CONFIG, ubah:
"dry_run": True,
```

---

## Struktur Kolom Spreadsheet

| Kolom | Isi                  |
|-------|----------------------|
| A     | IP Server            |
| B     | IP Rubrik            |
| C     | Database Name ← sumber nama DB |
| D     | Shot Date            |
| E     | Backup Date          |
| F     | Insert Date          |
| G     | SLA Name             |
| H     | Backup Status        |
| I+    | Status per tanggal (01 Juni, 02 Juni, dst.) |
| Q     | 10 Juni (kolom hari ini — otomatis terdeteksi) |

Script akan otomatis mendeteksi kolom hari ini berdasarkan header tanggal di baris 1.

---

## Status yang Ditulis ke Sheet

| Status         | Arti                                          |
|----------------|-----------------------------------------------|
| `DONE BACKUP`  | Backup berhasil hari ini                      |
| `ON PROGRESS`  | Sedang dicek (sementara, akan diupdate)       |
| `FAILED`       | Backup gagal di Rubrik                        |
| `NOT FOUND`    | DB tidak ditemukan di laporan Rubrik          |
| `OLD: YYYY-MM` | Backup ada tapi bukan hari ini                |

---

## Troubleshooting

**Login gagal?**
- Cek URL Rubrik di CONFIG (https atau http?)
- Rubrik biasanya self-signed cert — sudah ada `ignore_https_errors=True`
- Coba buka URL Rubrik manual di browser dulu

**Sheet tidak bisa diakses?**
- Pastikan file `service_account.json` ada di folder yang sama
- Pastikan email service account sudah di-share ke spreadsheet sebagai Editor

**DB tidak ditemukan di Rubrik?**
- Cek apakah nama di spreadsheet sama persis dengan yang di Rubrik
- Coba cari manual di Rubrik untuk konfirmasi nama yang benar
