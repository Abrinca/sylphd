// Browser check of the demo pipeline (Firefox or Chromium) with playwright-core.
//   python3 -m http.server -d demo 8799 &
//   PLAYWRIGHT_DIR=/path/to/node_modules node tests/browser_check.mjs [firefox|chromium] [--big | --only-big]
// Prints per-case stats and the SHA-256 of the k-mer entries (compare with the
// native sketches: the sim_* / k12 / FAM20866 values in the README).
// Needs the test fixtures in scratch/wasm (see README) and a browser binary:
// BROWSER_EXE overrides playwright's default location.
import { createRequire } from 'node:module';
const require = createRequire((process.env.PLAYWRIGHT_DIR || '.') + '/');
const kind = process.argv.includes('chromium') ? 'chromium' : 'firefox';
const { [kind]: browserType } = require('playwright-core');
const browser = await browserType.launch({ headless: true, executablePath: process.env.BROWSER_EXE });
console.log(kind, browser.version());
const page = await browser.newPage();
page.on('pageerror', (e) => console.log('pageerror', e.message));
page.on('console', (m) => { if (m.type() === 'error') console.log('console.error', m.text()); });
await page.goto('http://127.0.0.1:8799/index.html');
await page.evaluate(() => { document.getElementById('send').checked = false; });
const D = '/home/thomas/Projects/sylphd/';
const runs = process.argv.includes('--only-big') ? [] : [
  ['k12_single', [D + 'tests/data/k12.R1.fq.gz'], false],
  ['k12_twice', [D + 'scratch/wasm/k12_twice.fq.gz'], false],
  ['k12_paired', [D + 'tests/data/k12.R1.fq.gz', D + 'tests/data/k12.R2.fq.gz'], true],
  ['sim_bgzf', [D + 'scratch/wasm/sim_R1.bgzf.fq.gz'], false],
  ['sim_cat3', [D + 'scratch/wasm/sim_R1.cat3.fq.gz'], false],
  ['bad_crc', [D + 'scratch/wasm/bad_crc.fq.gz'], false],
  ['truncated', [D + 'scratch/wasm/truncated.fq.gz'], false],
];
if (process.argv.includes('--big')) runs.push(['FAM20866', ['/home/thomas/Projects/mindtherest/data/ont/FAM20866/reads.fastq.gz'], false]);
for (const [name, files, paired] of runs) {
  await page.evaluate((p) => { document.getElementById('paired').checked = p; }, paired);
  await page.setInputFiles('#file', files);
  try {
    await page.waitForFunction(() => window.__lastStats || window.__lastError, null, { timeout: 120000 });
  } catch (e) { console.log(name, 'TIMEOUT'); continue; }
  const r = await page.evaluate(async () => {
    const s = window.__lastStats;
    if (!s) return { error: window.__lastError };
    const buf = new Uint8Array(await window.__lastSketch.arrayBuffer());
    const n = Number(new DataView(buf.buffer).getBigUint64(0, true));
    const h = await crypto.subtle.digest('SHA-256', buf.subarray(0, 8 + 12 * n));
    return { reads: s.reads, kmers: s.kmers, sha: [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 12), wall: Math.round(s.wallMs), read: Math.round(s.readMs), gunzip: Math.round(s.gunzipMs), sketch: Math.round(s.sketchMs), crc: Math.round(s.crcMs), idle: Math.round(s.idleMs) };
  });
  console.log(name, JSON.stringify(r));
}
await browser.close();
