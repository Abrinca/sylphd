"""Sketch samples once at c=200,k=31 (the production databases' parameters) into .sylsp files.

usage: python -I make_sketches.py OUTDIR NAME:SPEC [NAME:SPEC ...]   (SPEC: file or R1+R2, as `sylphd profile --sample`)
"""
import sys, time, os
from sylphd import sketching

out = sys.argv[1]
os.makedirs(out, exist_ok=True)
for spec in sys.argv[2:]:
    s = sketching.parse_multi_file_spec(spec)
    t = time.time()
    sk = sketching.sketch_sample(s, c=200, k=31)
    b = sk.to_bytes()
    open(f"{out}/{s.name}.sylsp", "wb").write(b)
    print(f"{s.name}: {len(b)/1e6:.2f} MB sketch in {time.time()-t:.1f} s", flush=True)
