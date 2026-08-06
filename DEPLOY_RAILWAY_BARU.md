# Deploy ke Railway baru

1. Upload semua isi folder ini ke root repository GitHub.
2. Di Railway, pilih New Project > Deploy from GitHub Repo.
3. Root Directory harus kosong.
4. Build Command harus kosong.
5. Start Command harus kosong agar Railway memakai CMD dari Dockerfile.
6. Generate Domain pada Networking.
7. Untuk data akun Telegram yang tidak hilang saat redeploy, tambahkan Railway Volume lalu mount ke `/data` dan buat variable `BLASTER_DB_PATH=/data/blaster.db`.
