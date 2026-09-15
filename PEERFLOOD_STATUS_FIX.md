# PeerFlood / FloodWait status fix

Perubahan:

- Kolom jeda tersedia dan menerima nilai mulai dari 0 detik.
- Jeda mengikuti nilai pengguna dan dapat diatur ke 0 detik; batas Telegram tetap berlaku.
- PeerFlood dan FloodWait dibedakan dengan benar.
- Hanya target yang benar-benar dicoba yang ditandai gagal.
- Target sisanya ditandai `Dijeda — belum dikirim`, bukan gagal.
- Job berubah menjadi `Dijeda oleh Telegram`, tanpa retry otomatis.

Catatan: pembatasan PeerFlood berasal dari Telegram dan tidak dapat diselesaikan dengan menghapus jeda aplikasi.
