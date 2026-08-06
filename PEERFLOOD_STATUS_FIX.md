# PeerFlood / FloodWait status fix

Perubahan:

- Kolom jeda tidak lagi ditampilkan kepada pengguna.
- Aplikasi tetap menerapkan jeda internal 5 detik per akun.
- PeerFlood dan FloodWait dibedakan dengan benar.
- Hanya target yang benar-benar dicoba yang ditandai gagal.
- Target sisanya ditandai `Dijeda — belum dikirim`, bukan gagal.
- Job berubah menjadi `Dijeda oleh Telegram`, tanpa retry otomatis.

Catatan: pembatasan PeerFlood berasal dari Telegram dan tidak dapat diselesaikan dengan menghapus jeda aplikasi.
