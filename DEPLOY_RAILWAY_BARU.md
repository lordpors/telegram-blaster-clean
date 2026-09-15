# Deploy ke Railway baru

1. Upload semua isi folder ini ke root repository GitHub.
2. Di Railway, pilih New Project > Deploy from GitHub Repo.
3. Root Directory harus kosong.
4. Build Command harus kosong.
5. Start Command harus kosong agar Railway memakai CMD dari Dockerfile.
6. Generate Domain pada Networking.
7. Untuk data akun Telegram yang tidak hilang saat redeploy, tambahkan Railway Volume lalu mount ke `/data`. Aplikasi otomatis memakai `RAILWAY_VOLUME_MOUNT_PATH`; variable `BLASTER_DB_PATH` tidak lagi wajib.
8. Buat OAuth Client bertipe **Web application** di Google Cloud, lalu daftarkan callback `https://<domain>/auth/google/callback`.
9. Tambahkan variable Railway `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SESSION_SECRET`, `GOOGLE_REDIRECT_URI`, dan `BOOTSTRAP_OWNER_EMAIL`.
10. Untuk akses berbasis undangan, isi `GOOGLE_ALLOWED_EMAILS` dengan daftar email yang dipisahkan koma.

Data dari versi single-user otomatis diberikan kepada `BOOTSTRAP_OWNER_EMAIL` ketika migrasi pertama berjalan.
