/**
 * Pharma OS WhatsApp gateway.
 *
 * THIS FILE CONTAINS NO BUSINESS LOGIC AND MUST NEVER CONTAIN ANY.
 * It does exactly three things:
 *   1. receives WhatsApp messages and POSTs them to the Python API
 *   2. exposes POST /send and POST /send-document for the API to call back
 *   3. keeps the Baileys socket alive and reconnects
 *
 * The moment an `if (text === 'OK')` appears here, migrating to the official Meta
 * Cloud API stops being a one-file swap and becomes a rewrite. Resist.
 */
import express from 'express';
import pino from 'pino';
import qrcodeTerminal from 'qrcode-terminal';
import { Boom } from '@hapi/boom';
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
} from '@whiskeysockets/baileys';

const API_URL = process.env.API_URL || 'http://localhost:8000';
const SECRET = process.env.SHARED_SECRET;
const PORT = process.env.PORT || 3000;
// MUST point at a persistent volume, or you re-scan the QR on every deploy.
const AUTH_DIR = process.env.AUTH_DIR || '/data/auth';

if (!SECRET) {
  console.error('SHARED_SECRET is required');
  process.exit(1);
}

const log = pino({ level: process.env.LOG_LEVEL || 'info' });
let sock = null;
let ready = false;
let lastQR = null;

const jid = (phone) => `${String(phone).replace(/\D/g, '')}@s.whatsapp.net`;

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  log.info({ version }, 'starting baileys');

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: 'silent' }),
    markOnlineOnConnect: false,   // don't steal presence from the staff phone
    syncFullHistory: false,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      lastQR = qr;
      console.log('\n=== SCAN THIS QR WITH THE PHARMA OS WHATSAPP NUMBER ===\n');
      qrcodeTerminal.generate(qr, { small: true });
    }
    if (connection === 'open') {
      ready = true;
      lastQR = null;
      log.info('whatsapp connected');
    }
    if (connection === 'close') {
      ready = false;
      const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
      log.warn({ code }, 'connection closed');
      if (code === DisconnectReason.loggedOut) {
        log.error('logged out — delete the auth volume and re-scan the QR');
      } else {
        setTimeout(start, 4000);   // reconnect with a small backoff
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const m of messages) {
      try {
        await forward(m);
      } catch (e) {
        log.error({ err: e.message }, 'forward failed');
      }
    }
  });
}

async function forward(m) {
  if (m.key.fromMe) return;                       // ignore our own sends
  if (m.key.remoteJid?.endsWith('@g.us')) return; // ignore groups
  if (m.key.remoteJid === 'status@broadcast') return;

  const from = m.key.remoteJid.split('@')[0];
  const waId = m.key.id;
  const msg = m.message || {};

  const imageMsg = msg.imageMessage || msg.documentMessage;
  const text =
    msg.conversation ||
    msg.extendedTextMessage?.text ||
    msg.imageMessage?.caption ||
    msg.documentMessage?.caption ||
    '';

  // ---- media path: stream the bytes to the API as multipart
  if (imageMsg) {
    const buffer = await downloadMediaMessage(m, 'buffer', {}, { logger: log });
    const form = new FormData();
    form.append('wa_id', waId);
    form.append('sender', from);
    form.append('msg_type', 'image');
    form.append('caption', text || '');
    form.append(
      'file',
      new Blob([buffer], { type: imageMsg.mimetype || 'image/jpeg' }),
      `upload.${(imageMsg.mimetype || 'image/jpeg').split('/')[1] || 'jpg'}`,
    );
    const r = await fetch(`${API_URL}/webhook/media`, {
      method: 'POST',
      headers: { 'x-pharmaos-secret': SECRET },
      body: form,
    });
    log.info({ from, status: r.status }, 'media forwarded');
    return;
  }

  // ---- text path
  if (!text.trim()) return;
  const r = await fetch(`${API_URL}/webhook`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-pharmaos-secret': SECRET },
    body: JSON.stringify({ wa_id: waId, from, type: 'text', text }),
  });
  log.info({ from, status: r.status }, 'text forwarded');
}

// ---------------------------------------------------------------- HTTP server
const app = express();
app.use(express.json({ limit: '2mb' }));

app.use((req, res, next) => {
  if (req.path === '/health' || req.path === '/qr') return next();
  if (req.headers['x-pharmaos-secret'] !== SECRET) {
    return res.status(401).json({ error: 'bad secret' });
  }
  next();
});

app.get('/health', (_req, res) => res.json({ ok: true, ready, waitingForQR: !!lastQR }));

// Handy during setup: open this in a browser to see the pairing QR.
app.get('/qr', (_req, res) => {
  if (!lastQR) return res.send('<h3>No QR pending — already connected.</h3>');
  res.send(
    `<body style="display:grid;place-items:center;height:100vh;font-family:sans-serif">
       <div style="text-align:center">
         <h3>Scan with the Pharma OS WhatsApp number</h3>
         <img src="https://api.qrserver.com/v1/create-qr-code/?size=320x320&data=${encodeURIComponent(lastQR)}"/>
       </div>
     </body>`,
  );
});

app.post('/send', async (req, res) => {
  const { to, text } = req.body || {};
  if (!ready) return res.status(503).json({ error: 'whatsapp not connected' });
  if (!to || !text) return res.status(400).json({ error: 'to and text required' });
  try {
    await sock.sendMessage(jid(to), { text });
    res.json({ ok: true });
  } catch (e) {
    log.error({ err: e.message, to }, 'send failed');
    res.status(500).json({ error: e.message });
  }
});

// Forwards a prescription image to the pharmacist's own phone. Their native
// WhatsApp viewer has pinch-zoom, rotate and fullscreen — better than anything
// we would build, and they can verify from the dispensing bench.
app.post('/send-image', async (req, res) => {
  const { to, url, caption } = req.body || {};
  if (!ready) return res.status(503).json({ error: 'whatsapp not connected' });
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`fetch ${resp.status}`);
    const buffer = Buffer.from(await resp.arrayBuffer());
    await sock.sendMessage(jid(to), { image: buffer, caption: caption || '' });
    res.json({ ok: true });
  } catch (e) {
    log.error({ err: e.message, to }, 'send-image failed');
    res.status(500).json({ error: e.message });
  }
});

app.post('/send-document', async (req, res) => {
  const { to, url, filename, caption } = req.body || {};
  if (!ready) return res.status(503).json({ error: 'whatsapp not connected' });
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`fetch ${resp.status}`);
    const buffer = Buffer.from(await resp.arrayBuffer());
    await sock.sendMessage(jid(to), {
      document: buffer,
      fileName: filename || 'document.pdf',
      mimetype: 'application/pdf',
      caption: caption || '',
    });
    res.json({ ok: true });
  } catch (e) {
    log.error({ err: e.message, to }, 'send-document failed');
    res.status(500).json({ error: e.message });
  }
});

app.listen(PORT, () => log.info(`gateway http on :${PORT}`));
start().catch((e) => {
  log.error({ err: e.message }, 'startup failed');
  process.exit(1);
});
