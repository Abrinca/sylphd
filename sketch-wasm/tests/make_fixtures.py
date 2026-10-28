"""Makes the gzip edge-case fixtures used by tests/browser_check.mjs in scratch/wasm/.

Run `cargo test --release` first (it writes target/synth/sim_R1.fq).
    python3 tests/make_fixtures.py
"""

import gzip
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "scratch" / "wasm"
OUT.mkdir(parents=True, exist_ok=True)
k12 = (ROOT / "tests/data/k12.R1.fq.gz").read_bytes()
k12_plain = gzip.decompress(k12)

(OUT / "k12.fq").write_bytes(k12_plain)
(OUT / "k12_twice.fq.gz").write_bytes(k12 + k12)  # cat a.gz a.gz
bad = bytearray(k12 + k12)
bad[len(k12) - 8] ^= 1  # CRC32 of the first member
(OUT / "bad_crc.fq.gz").write_bytes(bytes(bad))
(OUT / "truncated.fq.gz").write_bytes(k12[:-30])


def bgzf(data: bytes) -> bytes:
    """Real BGZF: 64 KiB blocks with the BC extra field, plus the EOF block."""
    out = bytearray()
    for i in range(0, len(data), 65280):
        block = data[i : i + 65280]
        c = zlib.compressobj(6, zlib.DEFLATED, -15)
        body = c.compress(block) + c.flush()
        out += b"\x1f\x8b\x08\x04\0\0\0\0\0\xff" + struct.pack("<HBBHH", 6, 66, 67, 2, len(body) + 25)
        out += body + struct.pack("<II", zlib.crc32(block), len(block))
    out += bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")
    return bytes(out)


(OUT / "k12_bgzf_like.fq.gz").write_bytes(bgzf(k12_plain))
sim = ROOT / "sketch-wasm/target/synth/sim_R1.fq"
if sim.exists():
    data = sim.read_bytes()
    (OUT / "sim_R1.bgzf.fq.gz").write_bytes(bgzf(data))
    lines = data.split(b"\n")
    n = len(lines) // 4
    cuts = [0, n // 3, 2 * n // 3, n]
    parts = [b"\n".join(lines[a * 4 : b * 4]) + b"\n" for a, b in zip(cuts, cuts[1:])]
    (OUT / "sim_R1.cat3.fq.gz").write_bytes(b"".join(gzip.compress(p) for p in parts))
print("fixtures in", OUT)
