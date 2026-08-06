# Telegram Blaster — Multi-Queue Upgrade

## Perubahan utama

- Tidak ada lagi `active_account` global yang membuat tab Chrome saling mengganti akun.
- Akun dipilih pada setiap job melalui `/blast?account_id=ID` atau checkbox di form.
- Satu lock per akun: akun yang sama tidak menjalankan dua pengiriman serentak.
- Satu lock per username: target yang sama tidak diproses dua worker pada saat bersamaan.
- Target duplikat dalam satu daftar dibuang otomatis.
- Target yang masih berada di job aktif lain ditandai **Dilewati**.
- Status real-time: belum dikirim, sedang mengirim, terkirim, gagal, dan dilewati.
- Riwayat total berhasil terkirim: 1 jam, 3 jam, 6 jam, 12 jam, 1 hari, 3 hari, 7 hari, dan 30 hari.
- Job tersimpan di SQLite dan pekerjaan pending dapat dilanjutkan setelah restart.
- Item yang sedang berstatus `sending` saat server mati tidak dikirim ulang otomatis untuk mencegah pesan ganda.

## Upgrade di VPS

```bash
cd /var/www/telegram_blaster
sudo systemctl stop telegram_blaster

# Backup database lama terlebih dahulu
cp blaster.db blaster.db.backup-$(date +%Y%m%d-%H%M%S)

# Salin isi ZIP versi baru ke folder aplikasi, lalu:
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl daemon-reload
sudo systemctl restart telegram_blaster
sudo systemctl status telegram_blaster
```

Pantau log:

```bash
journalctl -u telegram_blaster -f
```

Tabel `blast_jobs` dan `blast_recipients` dibuat otomatis saat aplikasi pertama kali dijalankan. Semua akun lama yang memiliki session akan otomatis tersedia kembali di pemilih akun.

## Catatan operasi

Jalankan hanya **1 worker Uvicorn**. File `Procfile` dan `telegram_blaster.service` sudah disetel `--workers 1`, karena lock akun/target berjalan di proses aplikasi. Gunakan daftar penerima yang memang mengizinkan pesan dan patuhi FloodWait Telegram; aplikasi akan menghentikan antrean akun saat pembatasan tersebut muncul.
