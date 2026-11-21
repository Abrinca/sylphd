// Loads web/app.js in a vm with stubbed browser globals and checks which hits count as GTDB taxonomy.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const stub = () => new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => '' : k === 'length' ? 0 : stub()),
  apply: () => stub(), construct: () => stub(), set: () => true,
});
const location = { hostname: 'x', protocol: 'https:', search: '', hash: '', href: 'http://x/' };
const window = {
  SylphdToken: stub(), SYLPHD_CONFIG: { apiBase: 'http://x' }, location,
  addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
};
const ctx = {
  window, document: stub(), location, localStorage: { getItem: () => null, setItem() {} }, console,
  fetch: () => new Promise(() => {}), URLSearchParams, URL, navigator: {}, history: {}, AbortController, Intl,
  setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
  Node: stub(), HTMLElement: stub(), Element: stub(), Event: stub(), Blob: stub(), FileReader: stub(),
  DOMParser: stub(), CustomEvent: stub(),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const { nonGtdb, isGtdbTaxonomySource, hitSource } = window.Sylphd;

const lineage = { domain: 'Bacteria', phylum: 'Mycoplasmatota', species: 'Mycoplasmoides genitalium' };
// GlobDB hit with no GTDB metadata (a MOTU40_… genome): GTDB taxonomy, still no accession page.
const motu = { taxonomy_source: 'GlobDB_r232', taxonomy: lineage, genome_id: 'MOTU40_002958' };
assert.equal(nonGtdb(motu), false);
assert.equal(nonGtdb({ ...motu, taxonomy_source: 'GlobDB_r226' }), false);
assert.equal(hitSource(motu), 'GlobDB_r232'); // identifier/link rule is unchanged
assert.equal(nonGtdb({ taxonomy_source: 'GTDB_r214', taxonomy: lineage }), false);
assert.equal(nonGtdb({ gtdb_taxonomy: 'd__Bacteria;s__X', taxonomy_source: 'GlobDB_r232' }), false);
for (const source of ['UHGV_default', 'FungiRefSeq-latest', 'SoilSMAG']) {
  assert.equal(nonGtdb({ taxonomy_source: source, taxonomy: lineage }), true, source);
  assert.equal(isGtdbTaxonomySource(source), false, source);
}
for (const source of ['GTDB', 'GTDB_r232', 'GlobDB_r232', 'globdb_r226']) assert.equal(isGtdbTaxonomySource(source), true, source);
assert.equal(isGtdbTaxonomySource(null), false);
process.exit(0);
