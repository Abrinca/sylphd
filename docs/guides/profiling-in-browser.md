# Profiling reads in the browser

The [web app](https://abrinca.github.io/sylphd/#reads) has two equal modes, and both sketch in your
browser: **Assembly** finds the closest genomes of an assembly (see
[Identifying an assembly](assembly.md#sketching-in-the-browser)) and **Reads** gives a taxonomic
profile of sequencing reads. In *Reads* mode you drop the reads on the page and nothing but a small
sketch leaves your computer.

!!! note "Reads never leave your computer"
    The reads are read, decompressed and sketched **inside your browser** (WebAssembly in Web
    Workers). Only the sketch, a list of k-mer hashes and counts of a few MB, is uploaded to the
    server's [`POST /profile`](../reference/http-api.md#post-query-and-post-profile). It contains no
    sequences, read names or qualities. The server does not store or log it.

## How to use it

1. Open the **Reads** tab (or `#reads` in the address) and wait for the server status to turn
   green. The page sketches with the server's `c` and `k`, which it reads from `GET /info`.
2. Drop FASTQ or FASTA files (plain or `.gz`), or choose them:
    - **one file**: single-end reads, or *interleaved pairs* (`R1, R2, R1, R2, …`);
    - **two files**: paired-end `R1` + `R2`. Names like `_R1`/`_R2`, `_1`/`_2` or `.R1`/`.R2` are
      recognised and put in order; *Swap R1 / R2* flips them, and *Two single-end files* combines
      them into one single-end sample instead. The page compares the name of the first record of each
      file (ignoring `/1`, `/2` and the Illumina comment after the first space) and warns if they
      differ; only the first pair is checked, not the rest of the files;
    - **three or more files** are combined into one single-end sample (for example lanes).
3. Check the sample name (it defaults to the file name without suffixes) and press **Sketch and
   profile**.

Under **Options** you can change the minimum ANI (default 95 %, as `sylph profile`) and the minimum
number of k-mers per genome (10), turn on *Estimate unknown fraction* (`--estimate-unknown`) or
choose *Only sketch* to keep the sketch without uploading it.

While it works the page shows the stages (reading and decompressing, sketching, uploading,
profiling), the bytes read, the reads, bases and sketch k-mers counted, the elapsed time and the
speed. **Cancel** stops the workers immediately.

## Results

- **Summary:** sample, reads (or read pairs) and bases, sketch size, genomes detected, timings and,
  with *Estimate unknown fraction*, the share of the sample the database does not cover.
- **Abundance:** a stacked bar and a sorted list per taxon. Choose the **rank** (domain to
  species), the **taxonomy** (GTDB or NCBI) and the **abundance** (taxonomic or sequence).
  Abundances are summed up the lineage on the page with the rules of
  [Taxonomic profiles](taxonomic-profiles.md#lineage-rules): internal gaps become
  `unclassified <ancestor>`, trailing gaps are dropped, and genomes without a name at the chosen rank
  are shown as *not classified at that rank*.
- **Detected genomes:** a sortable table with both abundances, the adjusted ANI (and its 5–95 %
  interval), the effective coverage, the GTDB species and lineage, the NCBI organism and taxid, and
  links to NCBI and GTDB.
- **Downloads:** the table as TSV (the same columns as `sylphd profile`), the response as JSON, the
  taxonomic profile as `.sylphmpa` (the same file as `sylphd profile --taxprof`, for the selected
  taxonomy) and the **sketch** itself as `<sample>.sylsp`.

The downloaded sketch works with the command line: `sylphd profile sample.sylsp` uploads it as it
is. The sketches are byte-identical to the ones `sylphd` makes with pysylph, apart from the order
of the k-mer entries.

## Re-uploading a sketch { #reupload }

A `.sylsp` file, whether you downloaded it from this page, or `sylph sketch`, pysylph or `sylphd`
wrote it, can be dropped on the Reads tab instead of reads (or on the Assembly tab, below). Nothing
is read or sketched: the page shows the file, reads its small header and checks it against the
server before uploading. The sketch must have the same `k` as the server and may not be sparser
(`c` no larger than the server's), otherwise the page says which `c` and `k` it was made with and
which the server uses. The sample name comes from the file name, the profile is the same as for the
reads. A result shows one sample, so **one `.sylsp` at a time** (the page says so if you drop
several), and a sketch cannot be mixed with reads. The *Upload* chip in the header is the largest
sketch the server takes: a 1.5 GB ONT `fastq.gz` sketches to about 6 MB, 56 GB of paired-end reads
to about 76 MB.

## Limits and what to expect

- **Speed:** a 1.55 GB gzipped ONT file (254k reads, 2.1 Gbp) is sketched in about 10 s in
  Chromium (pysylph: 6–7 s), then the server takes a few seconds. Firefox needs about 14 s.
  Decompression is the bottleneck, so plain FASTQ is not faster to sketch than gzip.
- **Memory:** WebAssembly in a browser tab is limited to 4 GB. Bacterial isolates and long-read
  samples need tens of MB; deep short-read metagenomes with hundreds of millions of distinct k-mers
  can reach the limit. The page warns when the sketcher uses more than 1.5 GB and stops with a clear
  message before the limit. Use the command line for such samples.
- **Sketch size:** about 12 bytes per k-mer. The server (and its proxy) limit the upload; a
  `413` keeps the sketch on the page so that you can download it.
- **Errors:** rate limits (`429`) and a full queue (`503`) keep the sketch and offer *Upload
  again* after the `Retry-After` time, so nothing is sketched twice. Corrupt or truncated gzip
  files are reported with a message.
- **Browsers:** tested in Chromium and Firefox. WebAssembly SIMD and module workers are required
  (Chrome/Edge 91, Firefox 114, Safari 16.4 or newer; Safari is untested). The page needs no special
  HTTP headers.
- The sketcher uses sylph's seeding as it behaves on x86-64 with AVX2, like the pysylph the server
  runs; sketches from other CPUs can differ by up to three k-mers per read.

## The Assembly mode sketches the same way { #assembly-mode }

The Assembly tab uses the same WebAssembly sketcher: a pasted FASTA goes straight to it, a file
(plain or `.gz`, up to 2 GiB) through the same decompress and sketch workers.
The page counts the records, length and GC content while sketching, sends only the sketch to
[`POST /assembly`](../reference/http-api.md#post-assembly) (`Content-Type:
application/vnd.sylph.sketch`) and marks the result *sketched locally*. A 2.8 Mb assembly takes
about half a second, a 120 Mb pool of corrected reads about 2.5 s including the search. The hits are
identical to those of a server-side sketch of the same assembly.

A ready-made `.sylsp` sketch of an assembly can be dropped on the Assembly tab too. It is sent to
the same endpoint as it is, with the checks described under [re-uploading a sketch](#reupload). The
page cannot know the number of records, the length or the GC content of an assembly it did not
see, so the summary shows "–" for them, and the result is marked *uploaded sketch*.

## Building the sketcher

The sketcher is the Rust crate in [`sketch-wasm/`](https://github.com/Abrinca/sylphd/tree/main/sketch-wasm),
a minimal port of the code behind pysylph's `Sketcher`. The binaries are not committed: the Pages
workflow and `scripts/preview-pages.sh` build them into `web/wasm/` (needs `cargo`, the
`wasm32-unknown-unknown` target and `wasm-bindgen-cli` 0.2.129). Serving `web/` without that
directory still works; the Reads mode then says that the sketcher is missing.
