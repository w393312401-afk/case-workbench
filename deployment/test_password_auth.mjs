import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomBytes, scrypt } from 'node:crypto';
import { execFile } from 'node:child_process';
import { mkdtemp, writeFile, rename, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { createPasswordAuth } from './password_auth.mjs';

const password = 'Isolated test password 中文 2026!';
const salt = randomBytes(32);
const passwordHash = await promisify(scrypt)(password, salt, 64, { N: 32768, r: 8, p: 3, maxmem: 64 * 1024 * 1024 });
const baseAccount = {
  version: 1, username: 'fixture-owner', salt: salt.toString('hex'), password_hash: passwordHash.toString('hex'),
  scrypt: { n: 32768, r: 8, p: 3 }, credential_id: randomBytes(16).toString('hex'), updated_at: '2026-10-16T00:00:00Z',
};
const unauthorized = error => error.status === 401 && error.message === 'Invalid username or password.';
const cookieRequest = cookie => ({ headers: { cookie: cookie.split(';')[0] } });

async function fixture(t) {
  const dir = await mkdtemp(join(tmpdir(), 'workbench-password-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'account.json');
  let time = 1800000000000;
  const write = async value => {
    const temporary = join(dir, `account-${randomBytes(4).toString('hex')}.json`);
    await writeFile(temporary, typeof value === 'string' ? value : JSON.stringify(value));
    await rename(temporary, file);
  };
  await write(baseAccount);
  const auth = createPasswordAuth({ file, hostname: 'workbench.example.com', now: () => time });
  return { auth, file, write, advance: ms => { time += ms; } };
}

test('valid Unicode passwords create private opaque sessions and invalid credentials share one error', async t => {
  const { auth } = await fixture(t);
  assert.deepEqual(await auth.status(), { configured: true, username: baseAccount.username });
  for (const [username, value] of [
    [baseAccount.username, 'wrong'], ['missing-user', password], [baseAccount.username, ''],
    [baseAccount.username, 'x'.repeat(1025)], [null, password], [baseAccount.username, {}],
  ]) await assert.rejects(auth.login(username, value, 'fixture-client'), unauthorized);
  const login = await auth.login(baseAccount.username, password, 'fixture-client');
  assert.equal(login.username, baseAccount.username);
  assert.match(login.cookie, /^__Host-WorkbenchSession=[A-Za-z0-9_-]{43}; Path=\/; Secure; HttpOnly; SameSite=Lax; Max-Age=86400$/);
  assert.equal(login.cookie.includes(baseAccount.username), false);
  assert.equal(login.cookie.includes(password), false);
  assert.deepEqual(await auth.authenticate(cookieRequest(login.cookie)), { username: baseAccount.username });
  for (const req of [
    {}, { headers: {} }, cookieRequest('__Host-WorkbenchSession=made-up'),
    { headers: { cookie: `${login.cookie.split(';')[0]}; ${login.cookie.split(';')[0]}` } },
  ]) await assert.rejects(auth.authenticate(req), unauthorized);
  const logout = await auth.logout(cookieRequest(login.cookie));
  assert.match(logout.cookie, /Max-Age=0$/);
  await assert.rejects(auth.authenticate(cookieRequest(login.cookie)), unauthorized);
  assert.equal((await auth.logout({})).cookie, logout.cookie);
});

test('idle expiration is renewed by activity and the absolute lifetime is never extended', async t => {
  const { auth, advance } = await fixture(t);
  const first = cookieRequest((await auth.login(baseAccount.username, password, 'client-one')).cookie);
  advance(2 * 60 * 60 * 1000 - 1);
  await auth.authenticate(first);
  advance(2 * 60 * 60 * 1000);
  await assert.rejects(auth.authenticate(first), unauthorized);
  const active = cookieRequest((await auth.login(baseAccount.username, password, 'client-two')).cookie);
  for (let hour = 1; hour < 24; hour++) {
    advance(60 * 60 * 1000);
    await auth.authenticate(active);
  }
  advance(60 * 60 * 1000);
  await assert.rejects(auth.authenticate(active), unauthorized);
});

test('credential rotation, corruption and deletion revoke sessions; restoring the file does not restore sessions', async t => {
  const { auth, write, file } = await fixture(t);
  const first = cookieRequest((await auth.login(baseAccount.username, password, 'one')).cookie);
  const changed = { ...baseAccount, credential_id: randomBytes(16).toString('hex') };
  await write(changed);
  await assert.rejects(auth.authenticate(first), unauthorized);
  const second = cookieRequest((await auth.login(baseAccount.username, password, 'two')).cookie);
  await write('{invalid');
  await assert.rejects(auth.authenticate(second), unauthorized);
  assert.deepEqual(await auth.status(), { configured: false });
  await assert.rejects(auth.login(baseAccount.username, password, 'two'), unauthorized);
  await write(changed);
  await assert.rejects(auth.authenticate(second), unauthorized);
  const third = cookieRequest((await auth.login(baseAccount.username, password, 'three')).cookie);
  await rm(file);
  await assert.rejects(auth.authenticate(third), unauthorized);
  assert.match((await auth.logout(third)).cookie, /Max-Age=0$/);
  await write(changed);
  await assert.rejects(auth.authenticate(third), unauthorized);
});

test('an account edit with an accidentally reused credential ID still revokes sessions', async t => {
  const { auth, write } = await fixture(t);
  const first = cookieRequest((await auth.login(baseAccount.username, password, 'one')).cookie);
  await write({ ...baseAccount, username: 'renamed-owner' });
  await assert.rejects(auth.authenticate(first), unauthorized);
  await assert.rejects(auth.login(baseAccount.username, password, 'one'), unauthorized);
  const second = await auth.login('renamed-owner', password, 'one');
  assert.deepEqual(await auth.authenticate(cookieRequest(second.cookie)), { username: 'renamed-owner' });
});

test('Python hashlib.scrypt credentials interoperate and password changes reject the old password', async t => {
  const { auth, write } = await fixture(t);
  const oldSession = cookieRequest((await auth.login(baseAccount.username, password, 'one')).cookie);
  const nextPassword = 'Isolated Python fixture 密码!';
  const code = `import hashlib,json; salt=bytes(range(32)); print(json.dumps({"salt":salt.hex(),"password_hash":hashlib.scrypt(${JSON.stringify(nextPassword)}.encode("utf-8"),salt=salt,n=32768,r=8,p=3,dklen=64,maxmem=64*1024*1024).hex()}))`;
  const { stdout } = await promisify(execFile)('python', ['-c', code], { windowsHide: true, maxBuffer: 16384 });
  await write({ ...baseAccount, ...JSON.parse(stdout), credential_id: randomBytes(16).toString('hex') });
  await assert.rejects(auth.authenticate(oldSession), unauthorized);
  await assert.rejects(auth.login(baseAccount.username, password, 'one'), unauthorized);
  const login = await auth.login(baseAccount.username, nextPassword, 'one');
  assert.deepEqual(await auth.authenticate(cookieRequest(login.cookie)), { username: baseAccount.username });
});

test('malformed or oversized account files fail closed without accepting alternate scrypt parameters', async t => {
  const { auth, write } = await fixture(t);
  for (const value of [
    { ...baseAccount, version: 2 }, { ...baseAccount, scrypt: { n: 2, r: 8, p: 3 } },
    { ...baseAccount, salt: 'invalid' }, { ...baseAccount, password_hash: 'invalid' },
    { ...baseAccount, credential_id: 'invalid' }, { ...baseAccount, username: '\r\n' },
    'x'.repeat(8193),
  ]) {
    await write(value);
    assert.deepEqual(await auth.status(), { configured: false });
    await assert.rejects(auth.authenticate({}), unauthorized);
  }
});

test('a password verification in flight cannot create a session after credential rotation', async t => {
  const { auth, write } = await fixture(t);
  const pending = auth.login(baseAccount.username, password, 'rotation-client');
  // status is serialized behind the initial file read; scrypt then runs asynchronously.
  await auth.status();
  await write({ ...baseAccount, credential_id: randomBytes(16).toString('hex') });
  await assert.rejects(pending, unauthorized);
});

test('per-client and global throttles have retry times and expire predictably', async t => {
  const { auth, advance } = await fixture(t);
  const attempts = Array.from({ length: 8 }, () => auth.login('missing', 'wrong', 'same-client'));
  for (const result of await Promise.allSettled(attempts)) assert.ok(unauthorized(result.reason));
  await assert.rejects(auth.login(baseAccount.username, password, 'same-client'), error => error.status === 429 && error.retryAfter === 900);
  advance(15 * 60 * 1000);
  await auth.login(baseAccount.username, password, 'same-client');
  const global = await Promise.allSettled(Array.from({ length: 59 }, (_, n) => auth.login('missing', 'wrong', `client-${n}`)));
  assert.ok(global.every(result => [401, 429].includes(result.reason?.status)));
  await assert.rejects(auth.login(baseAccount.username, password, 'new-client'), error => error.status === 429 && error.retryAfter === 60);
  advance(60 * 1000);
  await auth.login(baseAccount.username, password, 'new-client');
});

test('parallel logins stay responsive, bound their work queue, and issue independent sessions', async t => {
  const { auth } = await fixture(t);
  let heartbeat = 0;
  const timer = setInterval(() => heartbeat++, 5);
  t.after(() => clearInterval(timer));
  const results = await Promise.allSettled(Array.from({ length: 20 }, (_, n) => auth.login(baseAccount.username, password, `parallel-${n}`)));
  const completed = results.filter(result => result.status === 'fulfilled').map(result => result.value);
  assert.equal(completed.length, 18, 'two active hashes and sixteen queued hashes are allowed');
  assert.equal(results.filter(result => result.reason?.status === 429).length, 2);
  assert.ok(heartbeat > 2, 'password hashing does not block the event loop');
  assert.equal(new Set(completed.map(result => result.cookie)).size, completed.length);
  await auth.logout(cookieRequest(completed[0].cookie));
  await assert.rejects(auth.authenticate(cookieRequest(completed[0].cookie)), unauthorized);
  await auth.authenticate(cookieRequest(completed[1].cookie));
});
