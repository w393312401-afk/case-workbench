import { createHash, randomBytes, scrypt, timingSafeEqual } from 'node:crypto';
import { open, rename, stat, unlink } from 'node:fs/promises';
import { dirname, isAbsolute, join } from 'node:path';

const COOKIE = '__Host-WorkbenchSession';
const COOKIE_FLAGS = 'Path=/; Secure; HttpOnly; SameSite=Lax';
const SESSION_MS = 90 * 24 * 60 * 60 * 1000;
const RENEW_MS = 24 * 60 * 60 * 1000;
const CLIENT_WINDOW_MS = 15 * 60 * 1000;
const GLOBAL_WINDOW_MS = 60 * 1000;
const MAX_ACCOUNT_BYTES = 8192;
const MAX_SESSION_BYTES = 65536;
const MAX_SESSIONS = 128;
const SCRYPT = Object.freeze({ N: 32768, r: 8, p: 3, maxmem: 64 * 1024 * 1024 });
const digest = value => createHash('sha256').update(value).digest('hex');
const denied = () => Object.assign(new Error('Invalid username or password.'), { status: 401 });
const unavailable = () => Object.assign(new Error('Login state is temporarily unavailable. Please try again.'), { status: 503 });
const limited = seconds => Object.assign(new Error('Too many attempts. Try again later.'), {
  status: 429, retryAfter: Math.max(1, Math.ceil(seconds)),
});
const signature = info => `${info.dev}:${info.ino}:${info.size}:${info.mtimeNs}:${info.ctimeNs}`;
const sessionCookie = token => `${COOKIE}=${token}; ${COOKIE_FLAGS}; Max-Age=${SESSION_MS / 1000}`;

function validateAccount(value) {
  if (!value || Array.isArray(value) || value.version !== 1
      || typeof value.username !== 'string' || !value.username.trim() || value.username.length > 128
      || /[\x00-\x1f\x7f]/.test(value.username)
      || !/^[a-f0-9]{64}$/.test(value.salt) || !/^[a-f0-9]{128}$/.test(value.password_hash)
      || !/^[a-f0-9]{32}$/.test(value.credential_id)
      || value.scrypt?.n !== SCRYPT.N || value.scrypt?.r !== SCRYPT.r || value.scrypt?.p !== SCRYPT.p
      || typeof value.updated_at !== 'string' || !value.updated_at || value.updated_at.length > 64) throw denied();
  return Object.freeze({ ...value, scrypt: Object.freeze({ ...value.scrypt }) });
}

function sessionToken(request) {
  const header = request?.headers?.cookie;
  if (typeof header !== 'string' || header.length > 16384) return null;
  const matches = header.split(';').map(part => part.trim()).filter(part => part.startsWith(`${COOKIE}=`));
  if (matches.length !== 1) return null;
  const token = matches[0].slice(COOKIE.length + 1);
  return /^[A-Za-z0-9_-]{43}$/.test(token) ? token : null;
}

/** Local account file + durable opaque sessions. No network or public account management. */
export function createPasswordAuth({ file, hostname, now = Date.now }) {
  if (typeof file !== 'string' || !isAbsolute(file)) throw new Error('An absolute account file is required.');
  if (typeof hostname !== 'string' || !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$/.test(hostname)) {
    throw new Error('A public DNS hostname is required.');
  }
  const sessionFile = join(dirname(file), 'sessions.json');
  let sessions = new Map(), sessionRevision;
  const clients = new Map(), globalAttempts = [];
  const dummySalt = randomBytes(32), dummyHash = randomBytes(64);
  const queue = [];
  let active = 0, cached, fileSignature, operations = Promise.resolve();

  // Serialize account refreshes and durable session changes; password hashing stays asynchronous.
  function exclusive(action) {
    const pending = operations.then(action);
    operations = pending.catch(() => {});
    return pending;
  }

  async function readAccount() {
    let handle;
    try {
      const info = await stat(file, { bigint: true });
      const nextSignature = signature(info);
      if (!info.isFile() || info.size < 2n || info.size > BigInt(MAX_ACCOUNT_BYTES)) throw denied();
      if (cached && nextSignature === fileSignature) return cached;
      handle = await open(file, 'r');
      const before = await handle.stat({ bigint: true });
      const buffer = Buffer.alloc(MAX_ACCOUNT_BYTES + 1);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      const after = await handle.stat({ bigint: true });
      const current = await stat(file, { bigint: true });
      if (bytesRead > MAX_ACCOUNT_BYTES || BigInt(bytesRead) !== after.size
          || nextSignature !== signature(before) || nextSignature !== signature(after)
          || nextSignature !== signature(current)) throw denied();
      const value = validateAccount(JSON.parse(buffer.subarray(0, bytesRead).toString('utf8')));
      const revision = digest(JSON.stringify(value));
      if (!cached || revision !== cached.revision) cached = Object.freeze({ value, revision });
      fileSignature = nextSignature;
      return cached;
    } catch {
      cached = fileSignature = undefined;
      throw denied();
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }

  async function commitSessions(next, revision) {
    const temporary = `${sessionFile}.${randomBytes(12).toString('hex')}.tmp`;
    let handle;
    try {
      handle = await open(temporary, 'wx', 0o600);
      await handle.writeFile(JSON.stringify({ version: 1, hostname, accountRevision: revision,
        sessions: [...next].map(([hash, session]) => ({ hash, ...session })) }));
      await handle.sync();
      await handle.close();
      handle = undefined;
      await rename(temporary, sessionFile);
      sessions = next;
      sessionRevision = revision;
    } catch {
      throw unavailable();
    } finally {
      if (handle) await handle.close().catch(() => {});
      await unlink(temporary).catch(() => {});
    }
  }

  async function readSessions() {
    let handle;
    try {
      handle = await open(sessionFile, 'r');
      const info = await handle.stat();
      if (!info.isFile() || info.size < 2 || info.size > MAX_SESSION_BYTES) return null;
      const buffer = Buffer.alloc(MAX_SESSION_BYTES + 1);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      if (bytesRead !== info.size || bytesRead > MAX_SESSION_BYTES) return null;
      return JSON.parse(buffer.subarray(0, bytesRead).toString('utf8'));
    } catch (error) {
      if (error.code === 'ENOENT') return undefined;
      if (error.code) throw unavailable();
      return null;
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }

  async function ensureSessions(revision) {
    if (sessionRevision === revision) return;
    if (sessionRevision !== undefined) {
      await commitSessions(new Map(), revision);
      return;
    }
    const saved = await readSessions();
    if (saved === undefined) { sessionRevision = revision; return; }
    const restored = new Map();
    let valid = saved?.version === 1 && saved.hostname === hostname && saved.accountRevision === revision
      && Array.isArray(saved.sessions) && saved.sessions.length <= MAX_SESSIONS;
    if (valid) for (const session of saved.sessions) {
      if (!session || !/^[a-f0-9]{64}$/.test(session.hash) || restored.has(session.hash)
          || !Number.isSafeInteger(session.createdAt) || session.createdAt < 0
          || !Number.isSafeInteger(session.renewedAt) || session.renewedAt < session.createdAt) {
        valid = false; break;
      }
      restored.set(session.hash, { createdAt: session.createdAt, renewedAt: session.renewedAt });
    }
    if (!valid) { await commitSessions(new Map(), revision); return; }
    sessions = restored;
    sessionRevision = revision;
    pruneSessions(now());
  }

  async function loadAccount() {
    let account;
    try { account = await readAccount(); }
    catch (error) {
      // Persist revocation so restoring a missing/corrupt credential file cannot revive old sessions.
      sessions.clear();
      if (sessionRevision !== null) await commitSessions(new Map(), null);
      throw error;
    }
    await ensureSessions(account.revision);
    return account;
  }

  function consumeAttempt(clientKey) {
    const time = now();
    const key = typeof clientKey === 'string' && clientKey.length > 0 && clientKey.length <= 256 ? clientKey : 'unknown';
    while (globalAttempts.length && globalAttempts[0] <= time - GLOBAL_WINDOW_MS) globalAttempts.shift();
    for (const [client, attempts] of clients) {
      while (attempts.length && attempts[0] <= time - CLIENT_WINDOW_MS) attempts.shift();
      if (!attempts.length) clients.delete(client);
    }
    const attempts = clients.get(key) || [];
    let retry = 0;
    if (attempts.length >= 8) retry = Math.max(retry, attempts[0] + CLIENT_WINDOW_MS - time);
    if (globalAttempts.length >= 60) retry = Math.max(retry, globalAttempts[0] + GLOBAL_WINDOW_MS - time);
    if (!clients.has(key) && clients.size >= 4096) retry = Math.max(retry, GLOBAL_WINDOW_MS);
    if (retry) throw limited(retry / 1000);
    attempts.push(time);
    clients.set(key, attempts);
    globalAttempts.push(time);
  }

  function derive(password, salt) {
    return new Promise((resolve, reject) => {
      const start = () => {
        active++;
        scrypt(password, salt, 64, SCRYPT, (error, result) => {
          active--;
          queue.shift()?.();
          if (error) reject(denied());
          else resolve(result);
        });
      };
      if (active < 2) start();
      else if (queue.length < 16) queue.push(start);
      else reject(limited(2));
    });
  }

  function pruneSessions(time) {
    for (const [key, session] of sessions) {
      if (time >= session.renewedAt + SESSION_MS) sessions.delete(key);
    }
  }

  return Object.freeze({
    async authenticate(request) {
      return exclusive(async () => {
        const account = await loadAccount();
        const token = sessionToken(request);
        const time = now();
        pruneSessions(time);
        const key = token && digest(token);
        const session = key && sessions.get(key);
        if (!session) throw denied();
        // Keep insertion order aligned with activity for bounded least-recently-used eviction.
        sessions.delete(key);
        sessions.set(key, session);
        if (time >= session.renewedAt + RENEW_MS) {
          const next = new Map(sessions);
          next.set(key, { ...session, renewedAt: time });
          await commitSessions(next, account.revision);
          return { username: account.value.username, cookie: sessionCookie(token) };
        }
        return { username: account.value.username };
      });
    },

    async login(username, password, clientKey) {
      consumeAttempt(clientKey);
      const account = await exclusive(loadAccount).catch(error => { if (error.status === 503) throw error; return null; });
      const validPassword = typeof password === 'string' && password.length > 0 && Buffer.byteLength(password, 'utf8') <= 1024;
      const match = !!account && typeof username === 'string' && username === account.value.username;
      const actual = await derive(validPassword ? password : 'invalid input', match ? Buffer.from(account.value.salt, 'hex') : dummySalt);
      const expected = match ? Buffer.from(account.value.password_hash, 'hex') : dummyHash;
      const verified = timingSafeEqual(actual, expected);
      if (!verified || !match || !validPassword) throw denied();
      // The password may have changed while a queued/asynchronous scrypt was running.
      return exclusive(async () => {
        if (await loadAccount() !== account) throw denied();
        const time = now();
        pruneSessions(time);
        const next = new Map(sessions);
        while (next.size >= MAX_SESSIONS) next.delete(next.keys().next().value);
        const token = randomBytes(32).toString('base64url');
        next.set(digest(token), { createdAt: time, renewedAt: time });
        await commitSessions(next, account.revision);
        return { username: account.value.username, cookie: sessionCookie(token) };
      });
    },

    async logout(request) {
      return exclusive(async () => {
        const token = sessionToken(request);
        if (token) {
          await loadAccount().catch(error => { if (error.status !== 401) throw error; });
          const next = new Map(sessions);
          if (next.delete(digest(token))) await commitSessions(next, sessionRevision);
        }
        return { cookie: `${COOKIE}=; ${COOKIE_FLAGS}; Max-Age=0` };
      });
    },

    async status() {
      return exclusive(async () => {
        try {
          const account = await loadAccount();
          return { configured: true, username: account.value.username };
        } catch (error) {
          if (error.status === 503) throw error;
          return { configured: false };
        }
      });
    },
  });
}
