import init, { WasmGunzip } from './pkg/sylph_sketch.js';

self.onmessage = async ({ data: { module, file } }) => {
  await init({ module_or_path: module });
  const results = {};
  const log = (line, done = false) => self.postMessage({ line, done, results });

  async function bench(name, fn) {
    const t = performance.now();
    const [inBytes, outBytes] = await fn();
    const s = (performance.now() - t) / 1000;
    results[name] = { s, inMBs: inBytes / 1e6 / s, outMBs: outBytes / 1e6 / s, inBytes, outBytes };
    log(`${name}: ${s.toFixed(2)} s, ${(inBytes / 1e6 / s).toFixed(0)} MB/s compressed` + (outBytes ? `, ${(outBytes / 1e6 / s).toFixed(0)} MB/s decompressed` : ''));
  }

  await bench('read only (File.stream)', async () => {
    let n = 0;
    const r = file.stream().getReader();
    for (;;) { const { value, done } = await r.read(); if (done) break; n += value.byteLength; }
    return [n, 0];
  });
  await bench('DecompressionStream', async () => {
    let n = 0, out = 0;
    const counter = new TransformStream({ transform(c, ctl) { n += c.byteLength; ctl.enqueue(c); } });
    const r = file.stream().pipeThrough(counter).pipeThrough(new DecompressionStream('gzip')).getReader();
    for (;;) { const { value, done } = await r.read(); if (done) break; out += value.byteLength; }
    return [n, out];
  });
  for (const verify of [true, false]) {
    await bench(`WasmGunzip zlib-rs (crc ${verify ? 'on' : 'off'})`, async () => {
      let n = 0, out = 0;
      const gz = new WasmGunzip(2 << 20, verify ? 'inline' : 'off');
      const r = file.stream().getReader();
      for (;;) {
        const { value, done } = await r.read();
        if (done) break;
        n += value.byteLength;
        gz.push(value);
        let o;
        while ((o = gz.pull()) !== undefined) out += o.byteLength;
      }
      out += gz.finish().byteLength;
      return [n, out];
    });
  }
  log('done', true);
};
