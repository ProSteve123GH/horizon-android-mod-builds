// Horizon Android mod builds — compile service.
//
// POST /v1/build?version=<V>   Seals a mod bundle (AES-256-CTR), stores it as a
//                              randomly named asset in the `requests` release,
//                              and dispatches build.yml in this repository.
//                              The job id is the first 24 hex of the SHA-256 of
//                              "/v1/build?version=<V>&commit=<C>" + body, where
//                              <C> is the commit versions.json pins for <V> —
//                              identical mods on the same commit are one job,
//                              built at most once.
// GET  /v1/job/<24-hex>        The job's status: 200 with the status document
//                              when the build published one, 202 while pending.
// GET  /v1/job/<24-hex>/lib    The compiled library (application/octet-stream),
//                              404 when there is none.
// GET  /                       Service info.
//
// GitHub is the build machine's backend; this worker only seals, looks up job
// results, and dispatches. Builds run in build.yml; publishing happens in a
// separate job that never executes mod code.
//
// Quotas (per worker isolate, in-memory — approximate; GitHub's own Actions
// concurrency is the hard backstop): 32 builds per address per UTC day, and
// 1500 builds globally per UTC day. Bodies are capped at 32 MiB.

const REPO_DEFAULT = 'ProSteve123GH/horizon-android-mod-builds';
const MAX_BODY = 32 * 1024 * 1024;
const DEFAULT_PER_ADDRESS = 32;
const DEFAULT_PER_DAY = 1500;
const VERSION_RE = /^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$/;
const JOB_RE = /^[0-9a-f]{24}$/;
const SHA_RE = /^[0-9a-f]{40}$/;

// --- small helpers ---------------------------------------------------------

function json(obj, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'access-control-allow-origin': '*',
      ...extraHeaders,
    },
  });
}

function hex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function unhex(s) {
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i += 1) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}

function utf8(s) {
  return new TextEncoder().encode(s);
}

function concat(...parts) {
  const len = parts.reduce((n, p) => n + p.byteLength, 0);
  const out = new Uint8Array(len);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.byteLength;
  }
  return out;
}

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
}

async function aesCtrEncrypt(keyBytes, ivBytes, plain) {
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'AES-CTR' }, false, [
    'encrypt',
  ]);
  // 128-bit counter starting at iv, big-endian — the same layout openssl uses
  // for -aes-256-ctr, so scripts/unseal.sh can decrypt with openssl.
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-CTR', counter: ivBytes, length: 128 },
    key,
    plain,
  );
  return new Uint8Array(ct);
}

function jobPath(version, commit) {
  return `/v1/build?version=${version}&commit=${commit}`;
}

// --- quota (per isolate, per UTC day; approximate by design) ----------------

const quota = { day: '', global: 0, byAddress: new Map() };

function quotaLimits(env) {
  const perAddress = Number.parseInt(env?.PER_ADDRESS ?? '', 10) || DEFAULT_PER_ADDRESS;
  const perDay = Number.parseInt(env?.PER_DAY ?? '', 10) || DEFAULT_PER_DAY;
  return { perAddress, perDay };
}

// Returns null when the address may proceed, or 'address' / 'global' when the
// day's budget is spent.
function quotaCheck(ip, env) {
  const today = new Date().toISOString().slice(0, 10);
  if (quota.day !== today) {
    quota.day = today;
    quota.global = 0;
    quota.byAddress.clear();
  }
  const { perAddress, perDay } = quotaLimits(env);
  if ((quota.byAddress.get(ip) || 0) >= perAddress) return 'address';
  if (quota.global >= perDay) return 'global';
  return null;
}

function quotaCount(ip) {
  quota.byAddress.set(ip, (quota.byAddress.get(ip) || 0) + 1);
  quota.global += 1;
}

// Test hooks: the module is loaded once per Isolate/test process.
export function __resetQuotaForTests() {
  quota.day = '';
  quota.global = 0;
  quota.byAddress.clear();
}

// --- versions.json (cached briefly per isolate) ------------------------------

let versionsCache = { at: 0, data: null };
const VERSIONS_CACHE_MS = 60_000;

export function __resetVersionsCacheForTests() {
  versionsCache = { at: 0, data: null };
}

async function loadVersions(env, fetchImpl) {
  const now = Date.now();
  if (versionsCache.data && now - versionsCache.at < VERSIONS_CACHE_MS) {
    return versionsCache.data;
  }
  const repo = env?.REPO || REPO_DEFAULT;
  const res = await fetchImpl(`https://raw.githubusercontent.com/${repo}/main/versions.json`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`versions.json fetch failed: HTTP ${res.status}`);
  const data = await res.json();
  versionsCache = { at: now, data };
  return data;
}

// --- GitHub API helpers -------------------------------------------------------

const API = 'https://api.github.com';
const UPLOADS = 'https://uploads.github.com';

function ghHeaders(env) {
  const headers = {
    accept: 'application/vnd.github+json',
    'user-agent': 'horizon-android-mod-builds',
    'x-github-api-version': '2022-11-28',
  };
  if (env?.GITHUB_TOKEN) headers.authorization = `Bearer ${env.GITHUB_TOKEN}`;
  return headers;
}

async function findRelease(env, fetchImpl, tag) {
  const repo = env?.REPO || REPO_DEFAULT;
  const res = await fetchImpl(`${API}/repos/${repo}/releases/tags/${tag}`, {
    headers: ghHeaders(env),
    signal: AbortSignal.timeout(15_000),
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`release ${tag}: HTTP ${res.status}`);
  return res.json();
}

async function findAsset(env, fetchImpl, tag, name) {
  const release = await findRelease(env, fetchImpl, tag);
  if (!release) return null;
  return release.assets?.find((a) => a.name === name) ?? null;
}

async function downloadAssetBytes(env, fetchImpl, asset) {
  const headers = { ...ghHeaders(env), accept: 'application/octet-stream' };
  const res = await fetchImpl(asset.url, {
    headers,
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`asset ${asset.name}: HTTP ${res.status}`);
  return new Uint8Array(await res.arrayBuffer());
}

async function downloadTextAsset(env, fetchImpl, asset) {
  return new TextDecoder().decode(await downloadAssetBytes(env, fetchImpl, asset));
}

async function uploadAsset(env, fetchImpl, tag, name, bytes, contentType) {
  const release = await findRelease(env, fetchImpl, tag);
  if (!release) throw new Error(`release ${tag} is missing`);
  const repo = env?.REPO || REPO_DEFAULT;
  const res = await fetchImpl(
    `${UPLOADS}/repos/${repo}/releases/${release.id}/assets?name=${encodeURIComponent(name)}`,
    {
      method: 'POST',
      headers: { ...ghHeaders(env), 'content-type': contentType },
      body: bytes,
      signal: AbortSignal.timeout(60_000),
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`asset upload ${name}: HTTP ${res.status} ${text}`.slice(0, 300));
  }
  return res.json();
}

async function dispatchBuild(env, fetchImpl, inputs) {
  if (!env?.GITHUB_TOKEN) throw new Error('GITHUB_TOKEN is not configured');
  const repo = env?.REPO || REPO_DEFAULT;
  const res = await fetchImpl(`${API}/repos/${repo}/actions/workflows/build.yml/dispatches`, {
    method: 'POST',
    headers: { ...ghHeaders(env), 'content-type': 'application/json' },
    body: JSON.stringify({ ref: 'main', inputs }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`workflow dispatch: HTTP ${res.status} ${text}`.slice(0, 300));
  }
}

// The status document for a job: `<job>.json` (build published) wins, then
// `<job>.failed.json` (settled failure), else null (pending / never built).
async function loadStatus(env, fetchImpl, job) {
  const tag = `results-${job[0]}`;
  const done = await findAsset(env, fetchImpl, tag, `${job}.json`);
  if (done) {
    try {
      const status = JSON.parse(await downloadTextAsset(env, fetchImpl, done));
      return { ...status, state: status.state || 'done' };
    } catch {
      return { state: 'done', parse_error: true };
    }
  }
  const failed = await findAsset(env, fetchImpl, tag, `${job}.failed.json`);
  if (failed) {
    try {
      const status = JSON.parse(await downloadTextAsset(env, fetchImpl, failed));
      return { ...status, state: 'failed' };
    } catch {
      return { state: 'failed' };
    }
  }
  return null;
}

// --- routes -------------------------------------------------------------------

async function handleBuild(request, url, env, fetchImpl) {
  const version = url.searchParams.get('version');
  if (!version || !VERSION_RE.test(version)) {
    return json({ error: 'missing or invalid version' }, 400);
  }
  const declared = Number(request.headers.get('content-length') || '0');
  if (declared > MAX_BODY) return json({ error: 'body too large' }, 413);

  let body;
  try {
    body = new Uint8Array(await request.arrayBuffer());
  } catch {
    return json({ error: 'could not read body' }, 400);
  }
  if (body.byteLength === 0) return json({ error: 'empty body' }, 400);
  if (body.byteLength > MAX_BODY) return json({ error: 'body too large' }, 413);

  const ip = request.headers.get('cf-connecting-ip') || 'unknown';
  const limited = quotaCheck(ip, env);
  if (limited) {
    return json(
      { error: limited === 'address' ? 'daily build limit for this address reached' : 'daily build limit reached' },
      429,
    );
  }

  let versions;
  try {
    versions = await loadVersions(env, fetchImpl);
  } catch (e) {
    return json({ error: `versions unavailable: ${e.message}` }, 502);
  }
  const commit = versions?.[version];
  if (typeof commit !== 'string' || !SHA_RE.test(commit)) {
    return json({ error: 'unknown version' }, 400);
  }

  const path = jobPath(version, commit);
  const digest = await sha256(concat(utf8(path), body));
  const job = hex(digest).slice(0, 24);

  // Content-addressed reuse: a published result for this exact version,
  // commit and body is served without rebuilding.
  let cached = null;
  try {
    cached = await loadStatus(env, fetchImpl, job);
  } catch (e) {
    return json({ error: `result lookup failed: ${e.message}` }, 502);
  }
  // Only successful builds are reusable. Caching a failure made every retry
  // of the same .hnmod (same content address) return the old error without
  // ever rebuilding, forcing users to delete result tags to force a retry.
  if (cached && cached.state === 'done') {
    return json({ job, cached: true, ...cached }, 200);
  }

  // Seal the body: iv || ciphertext, AES-256-CTR. The key travels only in the
  // workflow dispatch inputs (masked in logs); the asset name is random.
  const keyBytes = crypto.getRandomValues(new Uint8Array(32));
  const iv = crypto.getRandomValues(new Uint8Array(16));
  const ask = hex(crypto.getRandomValues(new Uint8Array(16))); // 32 hex
  const key = hex(keyBytes); // 64 hex

  let sealed;
  try {
    const ct = await aesCtrEncrypt(keyBytes, iv, body);
    sealed = concat(iv, ct);
  } catch (e) {
    return json({ error: `seal failed: ${e.message}` }, 500);
  }

  try {
    await uploadAsset(env, fetchImpl, 'requests', `${ask}.bin`, sealed, 'application/octet-stream');
  } catch (e) {
    return json({ error: `could not store the request: ${e.message}` }, 502);
  }

  try {
    await dispatchBuild(env, fetchImpl, { job, ask, key, version });
  } catch (e) {
    // The sealed asset is left for clean.yml to reap.
    return json({ error: `could not start the build: ${e.message}`, job }, 502);
  }

  quotaCount(ip);
  return json({ job, state: 'pending' }, 202);
}

async function handleJob(url, env, fetchImpl) {
  const m = /^\/v1\/job\/([0-9a-f]{24})$/.exec(url.pathname);
  if (!m) return json({ error: 'expected /v1/job/<24 lowercase hex>' }, 400);
  const job = m[1];
  let status = null;
  try {
    status = await loadStatus(env, fetchImpl, job);
  } catch (e) {
    return json({ error: `result lookup failed: ${e.message}` }, 502);
  }
  if (status) return json({ job, ...status }, 200);
  return json({ job, state: 'pending' }, 202);
}

async function handleJobLib(url, env, fetchImpl) {
  const m = /^\/v1\/job\/([0-9a-f]{24})\/lib$/.exec(url.pathname);
  if (!m) return json({ error: 'expected /v1/job/<24 lowercase hex>/lib' }, 400);
  const job = m[1];
  const tag = `results-${job[0]}`;

  let done;
  try {
    done = await findAsset(env, fetchImpl, tag, `${job}.json`);
  } catch (e) {
    return json({ error: `result lookup failed: ${e.message}` }, 502);
  }
  if (!done) return json({ error: 'no library for this job yet' }, 404);

  const lib = await findAsset(env, fetchImpl, tag, `${job}.so`);
  if (!lib) return json({ error: 'status exists but the library is missing' }, 404);

  const res = await fetchImpl(lib.url, {
    headers: { ...ghHeaders(env), accept: 'application/octet-stream' },
    signal: AbortSignal.timeout(120_000),
  });
  if (!res.ok) return json({ error: `library download failed: HTTP ${res.status}` }, 502);

  const headers = new Headers({
    'content-type': 'application/octet-stream',
    'access-control-allow-origin': '*',
    'x-job': job,
  });
  if (lib.size != null) headers.set('content-length', String(lib.size));
  return new Response(res.body, { status: 200, headers });
}

export async function handle(request, env, deps = {}) {
  const fetchImpl = deps.fetch ?? globalThis.fetch.bind(globalThis);
  const url = new URL(request.url);

  if (request.method === 'POST' && url.pathname === '/v1/build') {
    return handleBuild(request, url, env, fetchImpl);
  }
  if (request.method === 'GET' && url.pathname === '/') {
    return json({ service: 'horizon-android-mod-builds', status: 'ok' });
  }
  if (request.method === 'GET' && /^\/v1\/job\/[0-9a-f]{24}$/.test(url.pathname)) {
    return handleJob(url, env, fetchImpl);
  }
  if (request.method === 'GET' && /^\/v1\/job\/[0-9a-f]{24}\/lib$/.test(url.pathname)) {
    return handleJobLib(url, env, fetchImpl);
  }
  if (url.pathname.startsWith('/v1/job/')) {
    return json({ error: 'expected /v1/job/<24 lowercase hex>[/lib]' }, 400);
  }
  return json({ error: 'not found' }, 404);
}

export default {
  fetch(request, env) {
    return handle(request, env);
  },
};
