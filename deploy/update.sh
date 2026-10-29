#!/usr/bin/env bash
# sylphd-update — install or upgrade sylphd on a server.
#
#   sudo sylphd-update              # newest GitHub release
#   sudo sylphd-update v0.1.0       # a specific release (the "v" is optional)
#
# Installed as /usr/local/sbin/sylphd-update by setup-server.sh.  Run as root:
# it refreshes the uv-managed CPython, creates a fresh virtualenv under
# /srv/sylphd/releases/<version>-<timestamp>/, installs the wheels from the
# GitHub release after checking SHA256SUMS, switches /srv/sylphd/current
# atomically, restarts sylphd.service and waits for /health.  If the health
# check fails the previous release is restored and the script exits non-zero.
# Older releases beyond the newest three are pruned.
#
# Re-running is safe.  Environment knobs: SYLPHD_REPO, GH_TOKEN (raises the
# GitHub API rate limit), HEALTH_TIMEOUT.
set -euo pipefail
umask 022

REPO="${SYLPHD_REPO:-Abrinca/sylphd}"
ROOT=/srv/sylphd
RELEASES="$ROOT/releases"
CURRENT="$ROOT/current"
SOCKET="$ROOT/run/sylphd.sock"
SERVICE=sylphd.service
UV=/usr/local/bin/uv
PYTHON_SERIES=3.14
KEEP=3
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"

log() { printf '%s sylphd-update: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "error: $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo sylphd-update)"
[[ -x $UV ]] || die "$UV not found — run setup-server.sh first"
[[ -d $RELEASES ]] || die "$RELEASES not found — run setup-server.sh first"
command -v runuser >/dev/null || die "runuser not found"
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "$SERVICE is not installed — run setup-server.sh first"

# uv looks for uv.toml/pyproject.toml in the working directory; running from
# the home of the service account keeps it from reading root's files.
cd "$ROOT"

as_sylphd() {
    runuser -u sylphd -- env HOME="$ROOT" \
        UV_PYTHON_INSTALL_DIR="$ROOT/python" \
        PATH="/usr/local/bin:/usr/bin:/bin" "$@"
}

api_url="https://api.github.com/repos/$REPO/releases"
auth=()
[[ -n ${GH_TOKEN:-} ]] && auth=(-H "Authorization: Bearer $GH_TOKEN")

# --- resolve the release -----------------------------------------------------
tmp=""
release=""
keep_release=0
cleanup() {
    if [[ -n $tmp && -d $tmp ]]; then
        rm -rf "$tmp"
    fi
    # Drop a half-built release unless it is (or became) the live one.
    if [[ -n $release && $keep_release -ne 1 && -d $release ]]; then
        if [[ $(readlink -f "$CURRENT" 2>/dev/null) != $(readlink -f "$release") ]]; then
            rm -rf "$release"
        fi
    fi
}
trap cleanup EXIT

if [[ $# -gt 0 && -n ${1:-} ]]; then
    tag=$1
    [[ $tag == v* ]] || tag="v$tag"
    log "resolving release $tag"
    json=$(curl -fsSL --retry 3 "${auth[@]}" "$api_url/tags/$tag") ||
        die "cannot fetch release $tag from GitHub"
else
    log "resolving newest release"
    json=$(curl -fsSL --retry 3 "${auth[@]}" "$api_url/latest") ||
        die "cannot fetch the newest release from GitHub"
fi

tag=$(printf '%s' "$json" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])') ||
    die "could not parse the release metadata"
version=${tag#v}
[[ $tag =~ ^v[0-9] ]] || die "unexpected tag name: $tag"

tmp=$(mktemp -d "$RELEASES/.download.XXXXXX")
printf '%s' "$json" >"$tmp/release.json"

arch=$(uname -m)   # aarch64 / x86_64
/usr/bin/python3 - "$tmp/release.json" "$arch" >"$tmp/assets" <<'PY'
import json
import sys

release, arch = sys.argv[1], sys.argv[2]
assets = json.load(open(release))["assets"]
for asset in assets:
    name = asset["name"]
    lower = name.lower()
    wanted = (
        lower == "sha256sums"
        or (lower.startswith("sylphd-") and lower.endswith(".whl"))
        or (
            (lower.startswith("pysylph-") or lower.startswith("dnaio-"))
            and lower.endswith(".whl")
            and arch in lower
        )
    )
    if wanted:
        print(f'{name}\t{asset["browser_download_url"]}')
PY

grep -q '^SHA256SUMS' "$tmp/assets" || die "release $tag has no SHA256SUMS"
grep -q '^sylphd-' "$tmp/assets" || die "release $tag has no sylphd wheel"
grep -q '^pysylph-.*\.whl' "$tmp/assets" || die "release $tag has no pysylph wheel for $arch"

# --- download and verify -----------------------------------------------------
log "downloading release assets for $tag ($arch)"
while IFS=$'\t' read -r name url; do
    log "  $name"
    curl -fsSL --retry 3 -o "$tmp/$name" "$url" || die "download failed: $name"
done <"$tmp/assets"

log "verifying SHA256SUMS"
(
    cd "$tmp"
    for wheel in ./*.whl; do
        name=${wheel#./}
        grep -qF "  ./$name" SHA256SUMS || die "$name is not listed in SHA256SUMS"
    done
    sha256sum -c --ignore-missing SHA256SUMS
) || die "checksum verification failed; nothing was changed"
chmod 0755 "$tmp"   # root's staging directory; sylphd only reads the wheels

# --- build the release -------------------------------------------------------
log "refreshing CPython $PYTHON_SERIES to the newest patch release"
as_sylphd "$UV" python install "$PYTHON_SERIES" --upgrade ||
    die "could not install CPython $PYTHON_SERIES"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
release="$RELEASES/$tag-$stamp"
log "creating $release"
# releases/ is root's: create the directory here and hand it to sylphd to build in.
install -d -o sylphd -g sylphd -m 0755 "$release"
as_sylphd "$UV" venv --python "$PYTHON_SERIES" "$release" ||
    die "could not create the virtualenv"

log "installing sylphd==$version"
as_sylphd "$UV" pip install \
    --python "$release/bin/python" \
    --find-links "$tmp" \
    --only-binary pysylph,dnaio \
    --compile-bytecode \
    "sylphd==$version" || die "installation failed"

# The finished release belongs to root: the service runs it but cannot change
# it (hence --compile-bytecode above).  chown -R does not follow symlinks.
chown -R root:root "$release"

# setup-server.sh labels releases/ as usr_t (systemd's unconfined_service_t may
# only entrypoint bin_t/shell_exec_t/usr_t files); apply it to the new tree.
restorecon -R "$release" >/dev/null 2>&1 || true

# --- smoke test --------------------------------------------------------------
smoke=$(as_sylphd "$release/bin/sylphd" --version) || die "smoke test failed"
log "smoke test: $smoke"
grep -qF "$version" <<<"$smoke" || die "version mismatch: expected $version, got $smoke"

# --- switch and restart ------------------------------------------------------
previous=""
if [[ -L $CURRENT ]]; then
    previous=$(readlink "$CURRENT")
    [[ -d $previous ]] || previous=""
fi

log "switching $CURRENT -> $release"
ln -sfn "$release" "$CURRENT.next"
mv -Tf "$CURRENT.next" "$CURRENT"
keep_release=1

health_ok() {
    curl -fsS --max-time 3 --unix-socket "$SOCKET" http://localhost/health >/dev/null 2>&1
}

wait_health() {
    local i
    for ((i = 1; i <= HEALTH_TIMEOUT; i++)); do
        if health_ok; then
            log "health check passed after ${i}s"
            return 0
        fi
        sleep 1
    done
    return 1
}

log "restarting $SERVICE"
systemctl restart "$SERVICE" || log "restart returned non-zero; waiting for health anyway"

if ! wait_health; then
    log "health check failed within ${HEALTH_TIMEOUT}s"
    systemctl --no-pager status "$SERVICE" >&2 || true
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    if [[ -n $previous && -d $previous ]]; then
        log "rolling back to $previous"
        ln -sfn "$previous" "$CURRENT.next"
        mv -Tf "$CURRENT.next" "$CURRENT"
        systemctl restart "$SERVICE"
        if wait_health; then
            log "rollback healthy; new release left at $release for inspection"
        else
            log "rollback is NOT healthy either — check journalctl -u $SERVICE"
        fi
    else
        log "no previous release to roll back to; stopping $SERVICE"
        systemctl stop "$SERVICE" || true
    fi
    exit 1
fi

# --- prune -------------------------------------------------------------------
current_release=$(readlink -f "$CURRENT")
mapfile -t all < <(find "$RELEASES" -mindepth 1 -maxdepth 1 -type d \
    -name 'v[0-9]*-*' -printf '%T@ %f\n' | sort -n | cut -d' ' -f2-)
# Oldest first (by mtime, i.e. install time); drop everything beyond KEEP.
excess=$((${#all[@]} - KEEP))
for ((i = 0; i < excess; i++)); do
    old="$RELEASES/${all[i]}"
    if [[ $(readlink -f "$old") == "$current_release" ]]; then
        continue
    fi
    log "pruning $old"
    rm -rf "$old"
done

log "deployed sylphd $version: $release"
log "health: $(curl -fsS --unix-socket "$SOCKET" http://localhost/health)"
