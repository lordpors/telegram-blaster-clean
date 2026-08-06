const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const fs   = require('fs');
const path = require('path');

const OUT_FILE = process.argv[2];
const AUTH_DIR = `/tmp/wa_conn_${Date.now()}`;

function emit(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
process.on('uncaughtException',  e => { emit({ type: 'error', message: e.message }); process.exit(1); });
process.on('unhandledRejection', e => { emit({ type: 'error', message: String(e?.message || e) }); process.exit(1); });

let retries = 0;

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version }          = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version, auth: state,
    printQRInTerminal: false,
    logger: require('pino')({ level: 'silent' }),
    browser: ['MAMI GROUP', 'Chrome', '120.0.0'],
    connectTimeoutMs: 90_000,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async update => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      emit({ type: 'qr', data: qr });
      emit({ type: 'log', message: 'QR siap — scan dengan WhatsApp kamu' });
    }

    if (connection === 'open') {
      emit({ type: 'log', message: 'Login berhasil! Menyimpan sesi...' });

      const me    = sock.user;
      const phone = (me?.id || '').split(':')[0].split('@')[0] || 'unknown';

      // Tunggu saveCreds selesai menulis semua file
      await saveCreds();
      await new Promise(r => setTimeout(r, 2000));

      // Baca semua auth files
      const files = {};
      if (fs.existsSync(AUTH_DIR)) {
        for (const file of fs.readdirSync(AUTH_DIR)) {
          try { files[file] = fs.readFileSync(path.join(AUTH_DIR, file), 'utf8'); }
          catch {}
        }
      }
      emit({ type: 'log', message: `Sesi tersimpan (${Object.keys(files).length} file)` });

      fs.writeFileSync(OUT_FILE, JSON.stringify({ phone, files }));
      emit({ type: 'connected', phone: `+${phone}` });

      try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch {}
      await sock.end();
      process.exit(0);
    }

    if (connection === 'close') {
      const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        emit({ type: 'error', message: 'Logout — scan ulang QR' });
        process.exit(0);
      }
      if (retries < 5) {
        retries++;
        emit({ type: 'log', message: `Mencoba ulang (${retries}/5)...` });
        setTimeout(connect, 3000);
      } else {
        emit({ type: 'error', message: 'Gagal konek setelah 5 percobaan' });
        process.exit(1);
      }
    }
  });
}

emit({ type: 'log', message: 'Memulai koneksi...' });
connect().catch(e => { emit({ type: 'error', message: e.message }); process.exit(1); });
