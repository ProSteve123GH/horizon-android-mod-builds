// Unit tests for the worker: routing, job hashing, seal/unseal roundtrip,
// quota behaviour and versions.json failure handling. No network: every
// GitHub/raw fetch goes through an injected mock.

import {test, beforeEach} from 'node:test';
import assert from 'node:assert/strict';
import {createHash, createDecipheriv} from 'node:crypto';

import {handle, __resetQuotaForTests, __resetVersionsCacheForTests} from '../src/index.js';

const REPO = 'o/mod-builds';
const COMMIT = 'a'.repeat(40);
const VERSIONS = {'0.7.7': COMMIT};
const ip = (n) => `10.0.0.${n}`;

function env(overrides = {}) {
  return {REPO, GITHUB_TOKEN: 'tok', ...overrides};
}

function req(path, {method = 'GET', body, headers = {}} = {}) {
  return new Request(`https://worker.test${path}`, {
    method,
    body,
    headers: {'cf-connecting-ip': ip(1), ...headers},
  });
}

function sha256Hex(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function expectedJob(version, commit, body) {
  const path = `/v1/build?version=${version}&commit=${commit}`;
  return sha256Hex(Buffer.concat([Buffer.from(path, 'utf8'), Buffer.from(body)])).slice(0, 24);
}

// A mock fetch recording its calls and serving canned responses.
// routes: Array<{match: (url, init) => bool, respond: (url, init) => Response}>
function mockFetch(routes) {
  const calls = [];
  const fn = async (url, init = {}) => {
    const u = String(url);
    calls.push({url: u, init});
    for (const route of routes) {
      if (route.match(u, init)) return route.respond(u, init);
    }
    throw new Error(`unmocked fetch: ${u}`);
  };
  fn.calls = calls;
  return fn;
}

const versionsRoute = {
  match: (u) => u.startsWith('https://raw.githubusercontent.com/'),
  respond: () => new Response(JSON.stringify(VERSIONS), {status: 200}),
};
const notFoundRoute = (tag, name) => ({
  match: (u) => u === `https://api.github.com/repos/${REPO}/releases/tags/${tag}`,
  respond: () =>
    new Response(JSON.stringify({id: 7, tag_name: tag, assets: []}), {status: 200}),
});
const uploadRoute = {
  match: (u) => u.startsWith('https://uploads.github.com/'),
  respond: async (u, init) => new Response('{}', {status: 201}),
};
const dispatchRoute = {
  match: (u) => u.includes('/actions/workflows/build.yml/dispatches'),
  respond: async () => new Response(null, {status: 204}),
};

function happyDispatchFetch(extra = []) {
  const job = pendingCacheMissRoutes();
  return mockFetch([versionsRoute, ...job, ...extra, uploadRoute, dispatchRoute]);
}

function pendingCacheMissRoutes() {
  return [
    {
      match: (u) => u.includes('/releases/tags/results-'),
      respond: () => new Response(JSON.stringify({id: 9, assets: []}), {status: 200}),
    },
    {
      match: (u) => u.includes('/releases/tags/requests'),
      respond: () => new Response(JSON.stringify({id: 7, assets: []}), {status: 200}),
    },
  ];
}

beforeEach(() => {
  __resetQuotaForTests();
  __resetVersionsCacheForTests();
});

test('POST seals the body and dispatches a build', async () => {
  const fetchImpl = happyDispatchFetch();
  const body = Buffer.from('hello-mod-bytes');
  const res = await handle(
    req('/v1/build?version=0.7.7', {method: 'POST', body, headers: {'content-length': String(body.length)}}),
    env(),
    {fetch: fetchImpl},
  );
  assert.equal(res.status, 202);
  const data = await res.json();
  const bodyBytes = new Uint8Array(body);
  assert.match(data.job, /^[0-9a-f]{24}$/);
  assert.equal(data.job, expectedJob('0.7.7', COMMIT, bodyBytes));
  assert.equal(data.state, 'pending');

  const dispatch = fetchImpl.calls.find((c) => c.url.includes('/dispatches'));
  assert.ok(dispatch, 'workflow was dispatched');
  const inputs = JSON.parse(dispatch.init.body).inputs;
  assert.equal(inputs.job, data.job);
  assert.equal(inputs.version, '0.7.7');
  assert.match(inputs.ask, /^[0-9a-f]{32}$/);
  assert.match(inputs.key, /^[0-9a-f]{64}$/);

  // Seal/unseal roundtrip: the uploaded asset is iv || ciphertext, decryptable
  // with openssl-compatible AES-256-CTR.
  const upload = fetchImpl.calls.find((c) => c.url.includes('uploads.github.com'));
  assert.ok(upload, 'sealed asset was uploaded');
  const sealed = new Uint8Array(upload.init.body);
  const iv = sealed.slice(0, 16);
  const ct = sealed.slice(16);
  const key = Buffer.from(inputs.key, 'hex');
  const decipher = createDecipheriv('aes-256-ctr', key, iv);
  const plain = Buffer.concat([decipher.update(ct), decipher.final()]);
  assert.deepEqual(plain, body);
});

test('POST is answered from the results release when the job already exists', async () => {
  const body = Buffer.from('same-mods');
  const job = expectedJob('0.7.7', COMMIT, new Uint8Array(body));
  const tag = `results-${job[0]}`;
  const fetchImpl = mockFetch([
    versionsRoute,
    {
      match: (u) => u === `https://api.github.com/repos/${REPO}/releases/tags/${tag}`,
      respond: () =>
        new Response(
          JSON.stringify({assets: [{name: `${job}.json`, url: 'asset-json'}]}),
          {status: 200},
        ),
    },
    {
      match: (u) => u === 'asset-json',
      respond: () => new Response(JSON.stringify({state: 'done', sha256: 'ff'}), {status: 200}),
    },
  ]);
  const res = await handle(
    req('/v1/build?version=0.7.7', {method: 'POST', body}),
    env(),
    {fetch: fetchImpl},
  );
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.equal(data.job, job);
  assert.equal(data.cached, true);
  assert.equal(data.state, 'done');
  assert.ok(!fetchImpl.calls.some((c) => c.url.includes('/dispatches')), 'no rebuild dispatched');
});

test('POST reports a settled failure without rebuilding', async () => {
  const body = Buffer.from('broken-mods');
  const job = expectedJob('0.7.7', COMMIT, new Uint8Array(body));
  const tag = `results-${job[0]}`;
  const fetchImpl = mockFetch([
    versionsRoute,
    {
      match: (u) => u === `https://api.github.com/repos/${REPO}/releases/tags/${tag}`,
      respond: () =>
        new Response(
          JSON.stringify({assets: [{name: `${job}.failed.json`, url: 'asset-fail'}]}),
          {status: 200},
        ),
    },
    {
      match: (u) => u === 'asset-fail',
      respond: () => new Response(JSON.stringify({state: 'failed', error: 'compile error'}), {status: 200}),
    },
  ]);
  const res = await handle(
    req('/v1/build?version=0.7.7', {method: 'POST', body}),
    env(),
    {fetch: fetchImpl},
  );
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.equal(data.state, 'failed');
  assert.ok(!fetchImpl.calls.some((c) => c.url.includes('/dispatches')));
});

test('POST rejects an unknown version and dispatches nothing', async () => {
  const fetchImpl = mockFetch([versionsRoute]);
  const res = await handle(
    req('/v1/build?version=9.9.9', {method: 'POST', body: 'x'}),
    env(),
    {fetch: fetchImpl},
  );
  assert.equal(res.status, 400);
  assert.ok(!fetchImpl.calls.some((c) => c.url.includes('/dispatches')));
});

test('POST fails cleanly when versions.json cannot be fetched', async () => {
  const fetchImpl = mockFetch([
    {match: (u) => u.startsWith('https://raw.githubusercontent.com/'), respond: () => new Response('', {status: 500})},
  ]);
  const res = await handle(
    req('/v1/build?version=0.7.7', {method: 'POST', body: 'x'}),
    env(),
    {fetch: fetchImpl},
  );
  assert.equal(res.status, 502);
  assert.ok(!fetchImpl.calls.some((c) => c.url.includes('/dispatches')));
});

test('POST enforces the per-address quota', async () => {
  const fetchImpl = happyDispatchFetch();
  const small = Buffer.from('x');
  const mk = () =>
    req('/v1/build?version=0.7.7', {method: 'POST', body: small, headers: {'content-length': '1'}});
  const first = await handle(mk(), env({PER_ADDRESS: '2'}), {fetch: fetchImpl});
  const second = await handle(mk(), env({PER_ADDRESS: '2'}), {fetch: fetchImpl});
  assert.equal(first.status, 202);
  assert.equal(second.status, 202);
  const third = await handle(mk(), env({PER_ADDRESS: '2'}), {fetch: fetchImpl});
  assert.equal(third.status, 429);
});

test('POST enforces the global daily quota across addresses', async () => {
  const fetchImpl = happyDispatchFetch();
  const small = Buffer.from('x');
  const one = () =>
    req('/v1/build?version=0.7.7', {method: 'POST', body: small, headers: {'content-length': '1', 'cf-connecting-ip': ip(11)}});
  const two = () =>
    req('/v1/build?version=0.7.7', {method: 'POST', body: small, headers: {'content-length': '1', 'cf-connecting-ip': ip(12)}});
  const first = await handle(one(), env({PER_DAY: '1'}), {fetch: fetchImpl});
  assert.equal(first.status, 202);
  const second = await handle(two(), env({PER_DAY: '1'}), {fetch: fetchImpl});
  assert.equal(second.status, 429);
});

test('POST rejects oversized and empty bodies', async () => {
  const fetchImpl = mockFetch([versionsRoute]);
  const big = await handle(
    req('/v1/build?version=0.7.7', {method: 'POST', headers: {'content-length': String(33 * 1024 * 1024)}}),
    env(),
    {fetch: fetchImpl},
  );
  assert.equal(big.status, 413);

  const empty = await handle(req('/v1/build?version=0.7.7', {method: 'POST', body: ''}), env(), {
    fetch: fetchImpl,
  });
  assert.equal(empty.status, 400);
});

test('job hash matches the documented scheme', async () => {
  const fetchImpl = happyDispatchFetch();
  const body = Buffer.from('hash-me');
  const res = await handle(
    req('/v1/build?version=0.7.7', {method: 'POST', body}),
    env(),
    {fetch: fetchImpl},
  );
  const data = await res.json();
  const raw = sha256Hex(Buffer.concat([Buffer.from(`/v1/build?version=0.7.7&commit=${COMMIT}`), body]));
  assert.equal(data.job, raw.slice(0, 24));
});

test('GET /v1/job returns 202 while pending and 200 once published', async () => {
  const job = 'b'.repeat(24);
  const pending = mockFetch([
    {
      match: (u) => u.includes(`/releases/tags/results-${job[0]}`),
      respond: () => new Response(JSON.stringify({id: 9, assets: []}), {status: 200}),
    },
  ]);
  const r1 = await handle(req(`/v1/job/${job}`), env(), {fetch: pending});
  assert.equal(r1.status, 202);
  assert.equal((await r1.json()).state, 'pending');

  __resetVersionsCacheForTests();
  const published = mockFetch([
    {
      match: (u) => u.includes(`/releases/tags/results-${job[0]}`),
      respond: () =>
        new Response(JSON.stringify({assets: [{name: `${job}.json`, url: 'asset-json'}]}), {status: 200}),
    },
    {
      match: (u) => u === 'asset-json',
      respond: () => new Response(JSON.stringify({state: 'done', sha256: 'ee'}), {status: 200}),
    },
  ]);
  const r2 = await handle(req(`/v1/job/${job}`), env(), {fetch: published});
  assert.equal(r2.status, 200);
  assert.equal((await r2.json()).state, 'done');
});

test('GET /v1/job/<bad hex> is a 400, unknown path a 404', async () => {
  const fetchImpl = mockFetch([]);
  const bad = await handle(req('/v1/job/XYZ'), env(), {fetch: fetchImpl});
  assert.equal(bad.status, 400);
  const nf = await handle(req('/nope'), env(), {fetch: fetchImpl});
  assert.equal(nf.status, 404);
});

test('GET /v1/job/<id>/lib streams the library once published', async () => {
  const job = 'c'.repeat(24);
  const libb = Buffer.from('fake-so-bytes');
  const fetchImpl = mockFetch([
    {
      match: (u) => u.includes(`/releases/tags/results-${job[0]}`),
      respond: () =>
        new Response(
          JSON.stringify({
            assets: [
              {name: `${job}.json`, url: 'asset-json'},
              {name: `${job}.so`, url: 'asset-lib', size: libb.length},
            ],
          }),
          {status: 200},
        ),
    },
    {match: (u) => u === 'asset-lib', respond: () => new Response(libb, {status: 200})},
  ]);
  const res = await handle(req(`/v1/job/${job}/lib`), env(), {fetch: fetchImpl});
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('content-type'), 'application/octet-stream');
  assert.deepEqual(Buffer.from(await res.arrayBuffer()), libb);

  const missing = mockFetch([
    {
      match: (u) => u.includes(`/releases/tags/results-${job[0]}`),
      respond: () => new Response(JSON.stringify({id: 9, assets: []}), {status: 200}),
    },
  ]);
  const res404 = await handle(req(`/v1/job/${job}/lib`), env(), {fetch: missing});
  assert.equal(res404.status, 404);
});
