"""RAM-limit benchmark of `sylphd serve`: one run = one server under one memory limit.

usage: bench.py CONFIG LIMIT [--reps N] [--out results/DIR]

Paths: see the environment variables at the top of the code (RAMBENCH_WORK, SYLPHD_DB_DIR, SYLPHD_SRC,
SYLPHD_VENV_LIB, RAMBENCH_SKETCHES, DB_GTDB, DB_GLOBDB, DB_UHGV, DB_FUNGI).

  CONFIG  trio | globdb | gtdb | globdb-only   (see CONFIGS)
  LIMIT   GiB (e.g. 8, 4.5) or `unlimited`

The server runs in a rootless podman container with `--memory X --memory-swap X` (hard cgroup v2
limit, no swap), the databases bind-mounted read-only at /db.  The container's own cgroup is read
from /proc/<pid>/cgroup, so memory.peak / memory.current / memory.stat / memory.events are the
server's, page cache of the database files included.

Before every run the database files are evicted from the *host* page cache with
posix_fadvise(POSIX_FADV_DONTNEED) -- the bind mount is the same inode, so the container starts
cold.  They are evicted a second time right after /health answers, so the `cold` phase is really
cold (the startup itself has read large parts of the DBs into the cache).

Phases, all over the same pre-made sketches (make_sketches.py):
  startup   `podman run` -> /health answers
  cold      one sequential pass over all sketches, page cache just dropped
  warm-seq  REPS sequential passes
  warm-c2   REPS passes with 2 requests in flight (production: --max-jobs 2)

Appends one JSON object per run to OUT/runs.jsonl and one CSV row per request to OUT/requests.csv.
Everything the analysis needs is in those two files; `analyse.py` reads them.
"""
import argparse, concurrent.futures as cf, csv, hashlib, json, os, pathlib, statistics, subprocess, sys, threading, time

# Everything is configurable through environment variables; the defaults assume you run from the
# repository root of a checkout with `uv sync` done and the databases in ./db.
HERE = pathlib.Path(__file__).resolve().parent
env = lambda name, default: pathlib.Path(os.environ.get(name) or default).expanduser().resolve()
ROOT = env("RAMBENCH_WORK", "ram-bench-work")   # results/, logs/ and data/sketches/ live here
DB = env("SYLPHD_DB_DIR", "db")                 # directory with the database files
SRC = env("SYLPHD_SRC", HERE.parents[4])        # the sylphd source tree (a checkout), mounted read-only
SKETCHES = env("RAMBENCH_SKETCHES", ROOT / "data/sketches")
IMAGE = os.environ.get("RAMBENCH_IMAGE", "ghcr.io/astral-sh/uv:python3.14-trixie-slim")
# The venv's `python` is a symlink to the host's python, so it cannot run inside a container.
# Instead the container uses its own CPython 3.14 (same ABI, cp314) and gets the venv's
# site-packages plus the sylphd source.
VENV_LIB = env("SYLPHD_VENV_LIB", SRC / ".venv/lib/python3.14/site-packages")
ENTRY = "import sys; from sylphd.entry import main; sys.exit(main())"

GTDB = DB / os.environ.get("DB_GTDB", "gtdb-r232-c200-dbv2.syl2db")
GLOB = DB / os.environ.get("DB_GLOBDB", "globdb_r232_sylph_v2_c200.syl2db")
UHGV = DB / os.environ.get("DB_UHGV", "uhgv_c200_dbv1.syldb")
FUNGI = DB / os.environ.get("DB_FUNGI", "fungi-refseq-2025-10-11-c200-dbv1.syldb")
CONFIGS = {
    "trio": [GTDB, UHGV, FUNGI],
    "globdb": [GLOB, UHGV, FUNGI],
    "gtdb": [GTDB],
    "globdb-only": [GLOB],
}
GIB = 1 << 30


def evict(paths):
    for p in paths:
        fd = os.open(p, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


def fincore(paths):
    """Resident bytes of each file in the host page cache (util-linux fincore)."""
    out = {}
    for p in paths:
        r = subprocess.run(["fincore", "-b", "-n", "-o", "RES", str(p)], capture_output=True, text=True)
        tok = r.stdout.split()
        out[p.name] = int(tok[0]) if r.returncode == 0 and tok else None
    return out


def read_kv(path):
    d = {}
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            k, _, v = line.partition(" ")
            d[k] = int(v) if v.lstrip("-").isdigit() else v
    except FileNotFoundError:
        pass
    return d


def cgroup_of(pid):
    """/sys/fs/cgroup + the unified path of `pid` (empty when the process is gone)."""
    try:
        for line in pathlib.Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return pathlib.Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    except FileNotFoundError:
        pass
    return None


STAT_KEYS = ("anon", "file", "file_mapped", "active_file", "inactive_file", "shmem", "kernel",
             "slab", "pgmajfault", "pgscan", "pgsteal", "pgrefill", "workingset_refault_file",
             "workingset_refault_anon", "workingset_activate_file")


def snap(cg, pid):
    s = {}
    if cg is None:
        return {"error": "no cgroup"}
    try:
        s["limit_bytes"] = int(pathlib.Path(cg / "memory.max").read_text()) if \
            (pathlib.Path(cg / "memory.max").read_text().strip() != "max") else None
    except (FileNotFoundError, ValueError):
        pass
    try:
        s["current"] = int(pathlib.Path(cg / "memory.current").read_text())
    except (FileNotFoundError, ValueError):
        pass
    try:
        s["peak"] = int(pathlib.Path(cg / "memory.peak").read_text())
    except (FileNotFoundError, ValueError):
        pass
    st = read_kv(cg / "memory.stat")
    for k in STAT_KEYS:
        if k in st:
            s[k] = st[k]
    s["events"] = read_kv(cg / "memory.events")
    try:
        s["swap_current"] = int(pathlib.Path(cg / "memory.swap.current").read_text())
        s["swap_peak"] = int(pathlib.Path(cg / "memory.swap.peak").read_text())
    except (FileNotFoundError, ValueError):
        pass
    try:
        s["pressure"] = (cg / "memory.pressure").read_text().strip()
    except FileNotFoundError:
        pass
    io = read_kv(cg / "io.stat")
    rb = 0
    for k, v in io.items():
        if isinstance(v, str):
            for tok in v.split():
                if tok.startswith("rbytes="):
                    rb += int(tok.split("=")[1])
    s["io_rbytes"] = rb or None
    try:
        for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(("VmRSS", "VmHWM", "RssAnon", "RssFile")):
                k, v = line.split(":")
                s[k] = int(v.split()[0]) * 1024
    except (FileNotFoundError, ProcessLookupError):
        pass
    return s


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    i = (len(xs) - 1) * p / 100
    lo, hi = int(i), min(int(i) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def load_samples():
    """The pre-made sketches, smallest first (so a pass ramps up)."""
    files = sorted(SKETCHES.glob("*.sylsp"), key=lambda p: p.stat().st_size)
    return [(p.stem, p.read_bytes()) for p in files]


def podman(*args, check=False):
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=check)


def prune(out, config, limit_gib):
    """Drop earlier rows for the same config+limit, so a re-run replaces instead of duplicating."""
    p = pathlib.Path(out) / "runs.jsonl"
    if p.exists():
        keep = [ln for ln in p.read_text().splitlines() if ln.strip()
                and not (json.loads(ln).get("config") == config
                         and json.loads(ln).get("limit_gib") == limit_gib)]
        p.write_text("".join(ln + "\n" for ln in keep))
    q = pathlib.Path(out) / "requests.csv"
    if q.exists():
        rows = list(csv.reader(open(q)))
        head, body = rows[0], rows[1:]

        def same(r):
            if len(r) < 2 or r[0] != config:
                return False
            try:
                return float(r[1]) == limit_gib
            except ValueError:
                return r[1] == "" and limit_gib is None

        with open(q, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(head)
            w.writerows([r for r in body if not same(r)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config"); ap.add_argument("limit")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--startup-timeout", type=float, default=900)
    ap.add_argument("--max-jobs", type=int, default=2)
    ap.add_argument("--tag", default="")
    ap.add_argument("--replace", action="store_true",
                    help="drop earlier rows for the same config+limit instead of appending")
    ap.add_argument("--no-cold-evict", action="store_true",
                    help="do not drop the page cache after startup (cold phase becomes reuse-warm)")
    a = ap.parse_args()

    sys.path.insert(0, str(SRC / "src"))
    from sylphd.client import SylphClient

    dbs = CONFIGS[a.config]
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    limit = None if a.limit == "unlimited" else int(float(a.limit) * GIB)
    unit = f"ramb-{a.config}-{a.limit.replace('.', '_')}-{os.getpid()}"
    samples = load_samples()
    run = dict(config=a.config, limit_gib=None if limit is None else limit / GIB, tag=a.tag,
               dbs=[d.name for d in dbs], db_bytes={d.name: d.stat().st_size for d in dbs},
               samples=[(n, len(b)) for n, b in samples], reps=a.reps, max_jobs=a.max_jobs,
               runtime="podman", image=IMAGE, started=time.strftime("%F %T"),
               host_mem_available_start=read_kv("/proc/meminfo").get("MemAvailable"))
    podman("rm", "-f", unit)

    # ---- cold start: drop the DBs from the host page cache (same inodes as the bind mount) ----
    evict(dbs)
    time.sleep(1)
    run["fincore_before"] = fincore(dbs)

    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    logpath = ROOT / "logs" / f"{unit}.log"
    log = open(logpath, "w")
    cmd = ["podman", "run", "-d", "--name", unit, "--network", "host",
           "--security-opt", "label=disable",
           "-v", f"{VENV_LIB}:/opt/sylphd/lib:ro",
           "-v", f"{SRC}:{SRC}:ro",   # sylphd source, at the same path
           "-v", f"{DB}:/db:ro",
           "-e", f"PYTHONPATH=/opt/sylphd/lib:{SRC}/src",
           ]
    if limit is not None:
        cmd += ["--memory", str(limit), "--memory-swap", str(limit)]
    cmd += [IMAGE, "python3.14", "-c", ENTRY,
            "serve", "--tcp", f"127.0.0.1:{a.port}", "--max-jobs", str(a.max_jobs)]
    for d in dbs:
        cmd += ["--db", f"{d.stem}=/db/{d.name}"]

    t0 = time.monotonic()
    rc = podman(*cmd[1:])                   # cmd[0] is "podman" itself
    run["podman_run_rc"] = rc.returncode
    run["podman_run_err"] = rc.stderr.strip()[-400:]
    if rc.returncode != 0:
        print(f"podman run failed ({rc.returncode}): {rc.stderr.strip()}", flush=True)
    client = SylphClient(base_url=f"http://127.0.0.1:{a.port}", timeout=1800)

    peaks = []
    stop = threading.Event()
    cg_holder = {}

    def pid_of():
        r = podman("inspect", "--format", "{{.State.Pid}}", unit)
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None

    def sampler():
        while not stop.is_set():
            cg = cg_holder.get("cg")
            if cg:
                try:
                    cur = int((cg / "memory.current").read_text())
                    st = read_kv(cg / "memory.stat")
                    peaks.append((round(time.monotonic() - t0, 2), cur, st.get("anon"), st.get("file")))
                except Exception:
                    pass
            stop.wait(0.25)

    th = threading.Thread(target=sampler, daemon=True); th.start()

    pid = None
    for _ in range(50):                     # the container needs a moment to have a PID
        pid = pid_of()
        if pid:
            cg_holder["cg"] = cgroup_of(pid)
            if cg_holder["cg"]:
                break
        time.sleep(0.1)

    ready = False
    missing = 0
    while time.monotonic() - t0 < a.startup_timeout:
        if run["podman_run_rc"] != 0:
            break
        if pid is None:
            pid = pid_of()
            if pid:
                cg_holder["cg"] = cgroup_of(pid)
        status = podman("inspect", "--format", "{{.State.Status}}", unit).stdout.strip()
        if status == "exited":
            break                        # OOM-killed or crashed during the load
        if status == "":
            missing += 1                 # container not there (yet / any more)
            if missing > 20:
                break
        else:
            missing = 0
        try:
            client.health(); ready = True; break
        except Exception:
            time.sleep(0.1)
    cg = cg_holder.get("cg")
    run["startup_ok"] = ready
    run["startup_s"] = round(time.monotonic() - t0, 2)
    run["container_status_at_startup"] = podman("inspect", "--format", "{{.State.Status}}", unit).stdout.strip()
    run["oom_killed"] = podman("inspect", "--format", "{{.State.OOMKilled}}", unit).stdout.strip() == "true"
    run["after_startup"] = snap(cg, pid) if cg else {"error": "cgroup gone"}
    run["logs_startup"] = podman("logs", unit).stderr.strip()[-4000:]

    phases = {}
    rows = []
    results_digest = {}

    def cgroup_io_rbytes():
        if not cg:
            return None
        try:
            rb = 0
            for line in (cg / "io.stat").read_text().splitlines():
                for tok in line.split():
                    if tok.startswith("rbytes="):
                        rb += int(tok.split("=")[1])
            return rb
        except (FileNotFoundError, ValueError):
            return None

    def cgroup_current():
        try:
            return int((cg / "memory.current").read_text())
        except (FileNotFoundError, ValueError, TypeError):
            return None

    def post(cl, name, data):
        io0 = cgroup_io_rbytes()
        t = time.monotonic()
        try:
            r = cl.profile([(name, data)])
            ok = isinstance(r, list) and len(r) == 1
            nres = len(r[0].get("rows", [])) if ok and isinstance(r[0], dict) else None
            digest = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()[:16] if ok else None
            err = ""
        except Exception as e:
            ok, nres, digest, err = False, None, None, repr(e)[:300]
        d = time.monotonic() - t
        io1 = cgroup_io_rbytes()
        io = (io1 - io0) if (io0 is not None and io1 is not None) else None
        return d, ok, nres, err, digest, io, cgroup_current()

    def phase(label, nconc, nrep):
        lat = []
        walls = []
        for rep in range(nrep):
            tw = time.monotonic()
            if nconc == 1:
                for n, b in samples:
                    d, ok, nres, err, dg, io, mem = post(client, n, b)
                    rows.append([label, rep, n, nconc, round(d, 4), ok, nres, err, dg, io, mem])
            else:
                clients = [SylphClient(base_url=f"http://127.0.0.1:{a.port}", timeout=1800) for _ in range(nconc)]
                order = list(samples) if rep % 2 == 0 else list(reversed(samples))
                free = list(clients); lock = threading.Lock()

                def one(item):
                    with lock:
                        cl = free.pop()
                    try:
                        return item[0], post(cl, *item)
                    finally:
                        with lock:
                            free.append(cl)
                with cf.ThreadPoolExecutor(nconc) as ex:
                    for n, (d, ok, nres, err, dg, io, mem) in ex.map(one, order):
                        rows.append([label, rep, n, nconc, round(d, 4), ok, nres, err, dg, io, mem])
                for c in clients:
                    c.close()
            walls.append(round(time.monotonic() - tw, 3))
        phases[label] = dict(wall_s=walls)

    if ready and pid:
        if not a.no_cold_evict:
            # startup read a lot of the DBs; drop it again so `cold` means cold
            evict(dbs)
            run["fincore_after_evict"] = fincore(dbs)
            run["after_evict"] = snap(cg, pid)
        try:
            phase("cold", 1, 1)
            run["fincore_after_cold"] = fincore(dbs)
            run["after_cold"] = snap(cg, pid)
            phase("warm-seq", 1, a.reps)
            run["after_warm_seq"] = snap(cg, pid)
            phase("warm-c2", 2, a.reps)
            run["after_warm_c2"] = snap(cg, pid)
            run["fincore_after"] = fincore(dbs)
        except Exception as e:
            run["workload_error"] = repr(e)
    run["server_alive_at_end"] = podman("inspect", "--format", "{{.State.Status}}", unit).stdout.strip() == "running"
    if cg:
        run["memory_events_final"] = read_kv(cg / "memory.events")
        run["final"] = snap(cg, pid)
    stop.set(); th.join()
    run["timeseries_peak_current"] = max((p[1] for p in peaks), default=None)
    run["timeseries_peak_anon"] = max((p[2] or 0 for p in peaks), default=None)
    run["timeseries_peak_file"] = max((p[3] or 0 for p in peaks), default=None)
    run["timeseries"] = peaks[:: max(1, len(peaks) // 400)]
    run["host_mem_available_end"] = read_kv("/proc/meminfo").get("MemAvailable")

    # summary stats per phase / sample
    summ = {}
    for label in ("cold", "warm-seq", "warm-c2"):
        r = [x for x in rows if x[0] == label and x[5]]
        summ[label] = dict(n=len(r), fail=len([x for x in rows if x[0] == label and not x[5]]),
                           median_s=statistics.median([x[4] for x in r]) if r else None,
                           p90_s=pct([x[4] for x in r], 90),
                           wall_s=phases.get(label, {}).get("wall_s"),
                           per_sample={
                               n: dict(n=len([x for x in r if x[2] == n]),
                                      median_s=statistics.median([x[4] for x in r if x[2] == n]),
                                      p90_s=pct([x[4] for x in r if x[2] == n], 90))
                               for n in sorted({x[2] for x in r})})
    run["summary"] = summ

    # determinism: the same sketch must give the same answer under every memory limit
    digests = {}
    for x in rows:
        if x[5]:
            digests.setdefault(x[2], set()).add(x[8])
    run["answer_digests"] = {k: sorted(v) for k, v in digests.items()}

    podman("rm", "-f", unit)
    log.write(f"{json.dumps(run)[:2000]}\n")
    log.close()
    if a.replace:
        prune(out, a.config, run["limit_gib"])
    with open(out / "runs.jsonl", "a") as f:
        f.write(json.dumps(run) + "\n")
    newfile = not (out / "requests.csv").exists()
    with open(out / "requests.csv", "a", newline="") as f:
        w = csv.writer(f)
        if newfile:
            w.writerow(["config", "limit_gib", "tag", "phase", "rep", "sample", "concurrency",
                        "latency_s", "ok", "n_rows", "error", "digest", "io_rbytes", "mem_current"])
        for r in rows:
            w.writerow([a.config, run["limit_gib"], a.tag] + r)
    ev = run.get("memory_events_final") or {}
    st = run["after_startup"]
    print(f"{a.config} limit={a.limit}: startup_ok={ready} {run['startup_s']}s "
          f"peak@startup={(st.get('peak') or 0)/GIB:.2f}GiB anon={(st.get('anon') or 0)/GIB:.2f} "
          f"oom_kill={ev.get('oom_kill')} alive={run['server_alive_at_end']} "
          + " ".join(f"{k}: med={v['median_s'] and round(v['median_s'],3)} "
                     f"p90={v['p90_s'] and round(v['p90_s'],3)} fail={v['fail']}"
                     for k, v in summ.items() if v['n'] or v['fail']), flush=True)


if __name__ == "__main__":
    main()
