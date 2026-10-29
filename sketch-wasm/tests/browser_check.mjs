// Browser check of the production pipeline (web/sketching.js and its workers, Firefox or Chromium)
// with playwright-core.
//   ./build.sh && cargo test --release && python3 tests/make_fixtures.py   (in sketch-wasm/)
//   python3 -m http.server -d ../web 8799 &                                (in sketch-wasm/)
//   PLAYWRIGHT_DIR=/path/to/node_modules node tests/browser_check.mjs [firefox|chromium] [--big | --only-big]
// Compares the SHA-256 of the k-mer entries of every sketch with the native sketch that
// `cargo test` wrote to target/equality, and expects the corrupt gzip fixtures to fail.
// Exits non-zero on a mismatch, an error or a timeout. Needs the fixtures in scratch/wasm,
// a browser binary (BROWSER_EXE overrides playwright's default location) and, for --big /
// --only-big, a large ONT FASTQ at scratch/big/ont.fastq.gz (also used by node_check.mjs --big).
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import path from 'node:path';

const require = createRequire((process.env.PLAYWRIGHT_DIR || '.') + '/');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const R = (f) => path.join(root, f);
const kind = process.argv.includes('chromium') ? 'chromium' : 'firefox';
const { [kind]: browserType } = require('playwright-core');
const browser = await browserType.launch({ headless: true, executablePath: process.env.BROWSER_EXE });
console.log(kind, browser.version());
const page = await browser.newPage();
page.on('pageerror', (e) => console.log('pageerror', e.message));
page.on('console', (m) => { if (m.type() === 'error') console.log('console.error', m.text()); });
// A bare page next to web/sketching.js (same origin, so its workers and ./wasm load as in production).
await page.route('**/browser-check.html', (route) => route.fulfill({
  contentType: 'text/html',
  body: '<input id="file" type="file" multiple><script src="sketching.js"></script>',
}));
await page.goto('http://127.0.0.1:8799/browser-check.html');

// [name, input files, paired, native sketch in target/equality (null: must fail)]
const small = [
  ['k12_single', ['tests/data/k12.R1.fq.gz'], false, 'k12_single'],
  ['k12_twice', ['scratch/wasm/k12_twice.fq.gz'], false, 'k12_twice_multimember_rawgz'],
  ['k12_paired', ['tests/data/k12.R1.fq.gz', 'tests/data/k12.R2.fq.gz'], true, 'k12_paired'],
  ['sim_bgzf', ['scratch/wasm/sim_R1.bgzf.fq.gz'], false, 'sim_single'],
  ['sim_cat3', ['scratch/wasm/sim_R1.cat3.fq.gz'], false, 'sim_single'],
  ['bad_crc', ['scratch/wasm/bad_crc.fq.gz'], false, null],
  ['truncated', ['scratch/wasm/truncated.fq.gz'], false, null],
];
const big = [['FAM20866', ['scratch/big/ont.fastq.gz'], false, 'fam20866_ont']];
const only = process.argv.includes('--only-big');
const runs = [...(only ? [] : small), ...(only || process.argv.includes('--big') ? big : [])];

// SHA-256 (12 hex digits) of the header and k-mer entries of a .sylsp, like the page computes it
const digest = (buf) => {
  const n = Number(new DataView(buf.buffer, buf.byteOffset).getBigUint64(0, true));
  return createHash('sha256').update(buf.subarray(0, 8 + 12 * n)).digest('hex').slice(0, 12);
};

let failed = 0;
for (const [name, files, paired, native] of runs) {
  const fail = (why) => { failed++; console.log('FAIL', name, why); };
  await page.setInputFiles('#file', files.map(R));
  const r = await page.evaluate(async (layout) => {
    try {
      const run = window.SylphdSketch.start({ files: [...document.getElementById('file').files], layout, c: 200, k: 31, name: 'check' });
      const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 120000));
      const { sketch, stats } = await Promise.race([run.done, timeout]);
      const n = Number(new DataView(sketch.buffer, sketch.byteOffset).getBigUint64(0, true));
      const h = await crypto.subtle.digest('SHA-256', sketch.subarray(0, 8 + 12 * n));
      const r = (x) => Math.round(x);
      return { sha: [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 12), reads: stats.reads, kmers: stats.kmers, gc: stats.gcBases, lifetime: r(stats.lifetimeMs), sketch: r(stats.sketchMs), crc: r(stats.crcMs), idle: r(stats.idleMs) };
    } catch (e) {
      return { error: e.message };
    }
  }, paired ? 'paired' : 'single');
  if (native === null) {
    if (r.error) console.log('OK  ', name, 'failed as expected:', r.error);
    else fail('should have failed');
    continue;
  }
  if (r.error) { fail(r.error); continue; }
  const want = digest(readFileSync(R(`sketch-wasm/target/equality/${native}.sylsp`)));
  console.log(r.sha === want ? 'OK  ' : 'FAIL', name, JSON.stringify(r));
  if (r.sha !== want) { failed++; console.log('     expected', want, `(${native})`); }
}
await browser.close();
if (failed) {
  console.log(`${failed} failed`);
  process.exit(1);
}
