const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
} = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const fs   = require('fs');
const path = require('path');

const SESSION_FILE = process.argv[2];
const JOB_FILE     = process.argv[3];
const AUTH_DIR     = `/tmp/wa_add_${Date.now()}`;

function emit(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
process.on('uncaughtException',  e => { emit({ type: 'error', message: e.message }); process.exit(1); });
process.on('unhandledRejection', e => { emit({ type: 'error', message: String(e?.message || e) }); process.exit(1); });

let retries   = 0;
const MAX_RET = 3;
let jobDone   = false;

async function main() {
  // Restore session
  const sessionData = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8'));
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  for (const [name, content] of Object.entries(sessionData.files || {})) {
    fs.writeFileSync(path.join(AUTH_DIR, name), content);
  }
  emit({ type: 'log', message: `Sesi dipulihkan (${Object.keys(sessionData.files || {}).length} file)` });

  const job = JSON.parse(fs.readFileSync(JOB_FILE, 'utf8'));
  connect(job);
}

async function connect(job) {
  emit({ type: 'log', message: 'Menghubungkan akun...' });

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version }          = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version, auth: state,
    printQRInTerminal: false,
    logger: require('pino')({ level: 'silent' }),
    browser: ['MAMI GROUP', 'Chrome', '120.0.0'],
    connectTimeoutMs: 60_000,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async update => {
    const { connection, lastDisconnect } = update;

    if (connection === 'open') {
      retries = 0;
      emit({ type: 'log', message: 'Akun terhubung!' });
      await new Promise(r => setTimeout(r, 1500));

      // ── Ambil daftar group ──────────────────────────────────────
      if (job.mode === 'list_groups') {
        try {
          const groups    = await sock.groupFetchAllParticipating();
          const groupList = Object.entries(groups).map(([jid, meta]) => ({
            id: jid, name: meta.subject || jid, count: meta.participants?.length || 0,
          }));
          emit({ type: 'groups', data: groupList });
          emit({ type: 'log', message: `${groupList.length} group ditemukan` });
        } catch (e) {
          emit({ type: 'error', message: `Gagal ambil group: ${e.message}` });
        }
        jobDone = true;
        try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch {}
        await sock.end();
        process.exit(0);
        return;
      }

      // ── Tambah member ────────────────────────────────────────────
      if (job.mode === 'add_members') {
        const { group_id, numbers } = job;
        const jids = numbers.map(n => `${n.replace(/\D/g, '')}@s.whatsapp.net`);
        const meta = await sock.groupMetadata(group_id);
        emit({ type: 'log', message: `Group: "${meta.subject}" (${meta.participants.length} member)` });
        emit({ type: 'log', message: `Menambahkan ${jids.length} nomor...` });

        let added = 0, failed = 0;

        for (const jid of jids) {
          const num = jid.split('@')[0];
          try {
            // Cek dulu apakah nomor ada di WA
            const [check] = await sock.onWhatsApp(jid) || [];
            if (!check?.exists) {
              failed++;
              emit({ type: 'result', number: `+${num}`, status: 'tidak_di_wa' });
              emit({ type: 'progress', added, failed, total: jids.length });
              await new Promise(r => setTimeout(r, 500));
              continue;
            }
            // Tambahkan satu per satu
            const results = await sock.groupParticipantsUpdate(group_id, [jid], 'add');
            const r = results?.[0];
            if (!r) throw new Error('no response');
            if (r.status === '200')      { added++; emit({ type: 'result', number: `+${num}`, status: 'added' }); }
            else if (r.status === '403') { failed++; emit({ type: 'result', number: `+${num}`, status: 'privacy' }); }
            else if (r.status === '409') { emit({ type: 'result', number: `+${num}`, status: 'already_member' }); }
            else                         { failed++; emit({ type: 'result', number: `+${num}`, status: `gagal(${r.status})` }); }
          } catch (e) {
            failed++;
            emit({ type: 'result', number: `+${num}`, status: `error: ${e.message}` });
          }
          emit({ type: 'progress', added, failed, total: jids.length });
          await new Promise(r => setTimeout(r, 1500));
        }

        emit({ type: 'done', added, failed, total: jids.length });
        jobDone = true;
        try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch {}
        await sock.end();
        process.exit(0);
        return;
      }
    }

    if (connection === 'close') {
      if (jobDone) { process.exit(0); return; }

      const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
      emit({ type: 'log', message: `Koneksi terputus (kode: ${code})` });

      // Loggedout → perlu scan ulang
      if (code === DisconnectReason.loggedOut || code === 401) {
        emit({ type: 'error', message: 'Sesi expired — hapus akun WA dan scan ulang QR' });
        process.exit(1);
        return;
      }

      // Error lain → retry
      if (retries < MAX_RET) {
        retries++;
        emit({ type: 'log', message: `Mencoba ulang koneksi (${retries}/${MAX_RET})...` });
        await new Promise(r => setTimeout(r, 4000));
        connect(job);
      } else {
        emit({ type: 'error', message: 'Gagal konek setelah beberapa percobaan. Coba hapus akun WA dan scan ulang QR.' });
        process.exit(1);
      }
    }
  });
}

main().catch(e => { emit({ type: 'error', message: e.message }); process.exit(1); });
