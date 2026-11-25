// Exercises the renderer / per-contig merge / api_version exports of the built
// wasm module, for tests/test_web_contigs_download.py.
//
//   node rust/sylphd-wasm/tests/render_check.mjs < payload.json
//
// stdin: {"batches": [...], "options": {...}} and optionally
//        {"profile": <doc>, "assembly": <doc>, "info": <doc>, "messages": [...]}
// stdout: the wasm exports' results as JSON. A throwing export becomes
//         {"error": "<JS exception text>"} under its key.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import {
  initSync, supported_api_version, check_api_version, render_tsv, render_profile_caveats,
  render_assembly_tsv, render_assembly_summary, render_assembly_stderr,
  merge_contigs_run, render_contigs_tsv, render_contigs_summary, render_info,
} from "../../../web/wasm/sylph_sketch.js";

const here = path.dirname(fileURLToPath(import.meta.url));
initSync({ module: readFileSync(path.join(here, "../../../web/wasm/sylph_sketch_bg.wasm")) });

const payload = JSON.parse(readFileSync(0, "utf8"));
const attempt = (f) => {
  try {
    return f();
  } catch (e) {
    return { error: String(e) };
  }
};

const out = {
  supported_api_version: supported_api_version(),
  api_version_1: attempt(() => check_api_version(1) ?? "ok"),
  api_version_2: attempt(() => check_api_version(2)),
};

if (payload.batches !== undefined) {
  out.merged = attempt(() => {
    const docJson = merge_contigs_run(
      JSON.stringify(payload.batches),
      payload.options === undefined ? undefined : JSON.stringify(payload.options),
    );
    return {
      doc: JSON.parse(docJson),
      tsv: render_contigs_tsv(docJson),
      summary: render_contigs_summary(docJson),
    };
  });
}
if (payload.profile !== undefined) {
  const docJson = JSON.stringify(payload.profile);
  out.profile = attempt(() => ({
    tsv: render_tsv(docJson),
    caveats: render_profile_caveats(docJson),
  }));
}
if (payload.assembly !== undefined) {
  const docJson = JSON.stringify(payload.assembly);
  out.assembly = attempt(() => ({
    tsv: render_assembly_tsv(docJson),
    summary: render_assembly_summary(docJson),
    stderr: render_assembly_stderr(docJson),
  }));
}
if (payload.info !== undefined) {
  out.info = attempt(() => render_info(
    JSON.stringify(payload.info),
    payload.messages === undefined ? undefined : JSON.stringify(payload.messages),
  ));
}
console.log(JSON.stringify(out));
