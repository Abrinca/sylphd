#!/usr/bin/env bash
# setup-server.sh — first-time setup / reconciliation of the sylphd host.
#
#   sudo ./setup-server.sh
#
# Idempotent: every step checks the current state first, so it is also the
# way to re-apply configuration after a change.  It lays out the service
# account and directories, installs uv and a uv-managed CPython, installs
# Caddy from the official COPR, writes the Caddyfile and the systemd units,
# applies the SELinux bits needed for Caddy (httpd_t) to reach sylphd's Unix
# socket, and enables the timers.
#
# It does NOT install sylphd itself — run `sylphd-update` afterwards (or
# `sylphd-update v0.1.0`).  Re-running this script never touches an existing
# release or the database.
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=/srv/sylphd
SYLPH_USER=sylphd
UV=/usr/local/bin/uv

log() { printf '%s setup-server: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "error: $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"

# ---------------------------------------------------------------------------
# 1. service account and directory layout
# ---------------------------------------------------------------------------
log "service account and directories"
getent group "$SYLPH_USER" >/dev/null || groupadd --system "$SYLPH_USER"
id -u "$SYLPH_USER" >/dev/null 2>&1 ||
    useradd --system --gid "$SYLPH_USER" --home-dir "$ROOT" \
        --shell /sbin/nologin "$SYLPH_USER"

install -d -o "$SYLPH_USER" -g "$SYLPH_USER" -m 0755 \
    "$ROOT" "$ROOT/db" "$ROOT/releases" "$ROOT/run" "$ROOT/python"
install -d -o "$SYLPH_USER" -g "$SYLPH_USER" -m 0700 \
    "$ROOT/.cache" "$ROOT/.local/share/uv"

# The database and metadata files are placed here by hand; only fix ownership
# of the directory (never rewrite the 20 GB file).
chown "$SYLPH_USER:$SYLPH_USER" "$ROOT" "$ROOT/db" "$ROOT/releases" "$ROOT/run" "$ROOT/python"

# ---------------------------------------------------------------------------
# 2. firewalld
# ---------------------------------------------------------------------------
if systemctl is-active --quiet firewalld; then
    changed=0
    for svc in http https; do
        if ! firewall-cmd --permanent --query-service="$svc" >/dev/null; then
            firewall-cmd --permanent --add-service="$svc"
            changed=1
        fi
    done
    if [[ $changed -eq 1 ]]; then
        firewall-cmd --reload
    fi
    log "firewalld: http/https open"
else
    log "firewalld is not active — skipping firewall rules"
fi

# ---------------------------------------------------------------------------
# 3. dnf-automatic (security and bugfix updates, reboot when needed)
# ---------------------------------------------------------------------------
log "dnf-automatic"
rpm -q dnf-automatic >/dev/null 2>&1 || dnf install -y dnf-automatic
automatic_conf=$(mktemp)
cat >"$automatic_conf" <<'EOF'
# Managed by sylphd setup-server.sh — see deploy/setup-server.sh.
[commands]
upgrade_type = default
random_sleep = 0
network_online_timeout = 60
download_updates = yes
apply_updates = yes
reboot = when-needed
reboot_command = "shutdown -r +5 'Rebooting after applying package updates'"

[emitters]
emit_via = stdio

[base]
debuglevel = 1
EOF
if cmp -s "$automatic_conf" /etc/dnf/automatic.conf; then
    rm -f "$automatic_conf"
else
    install -m 0644 "$automatic_conf" /etc/dnf/automatic.conf
    rm -f "$automatic_conf"
fi
systemctl enable --now dnf-automatic.timer >/dev/null 2>&1

# ---------------------------------------------------------------------------
# 4. uv (standalone installer, owned by root)
# ---------------------------------------------------------------------------
log "uv"
if [[ ! -x $UV ]]; then
    curl -fsSL https://astral.sh/uv/install.sh |
        env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
# uv updates itself by replacing the binary via a temp file; restore the label
# (the timer does the same after every self-update).
restorecon /usr/local/bin/uv /usr/local/bin/uvx 2>/dev/null || true

if [[ -f $HERE/uv-self-update.service && -f $HERE/uv-self-update.timer ]]; then
    install -m 0644 "$HERE/uv-self-update.service" /etc/systemd/system/uv-self-update.service
    install -m 0644 "$HERE/uv-self-update.timer" /etc/systemd/system/uv-self-update.timer
    systemctl daemon-reload
    systemctl enable --now uv-self-update.timer >/dev/null 2>&1
fi

# ---------------------------------------------------------------------------
# 5. CPython 3.14 managed by uv, as the sylphd user
# ---------------------------------------------------------------------------
log "CPython 3.14"
(cd "$ROOT" && runuser -u "$SYLPH_USER" -- env HOME="$ROOT" \
    UV_PYTHON_INSTALL_DIR="$ROOT/python" "$UV" python install 3.14)

# uv has no setting for the managed-Python directory, so expose it through the
# default discovery path too: plain `uv python find 3.14` then works whenever
# HOME=/srv/sylphd.
ln -sfn "$ROOT/python" "$ROOT/.local/share/uv/python"

cat >/etc/profile.d/sylphd-uv.sh <<'EOF'
# Managed CPython for the sylphd service account.
export UV_PYTHON_INSTALL_DIR=/srv/sylphd/python
EOF
chmod 0644 /etc/profile.d/sylphd-uv.sh

# ---------------------------------------------------------------------------
# 6. Caddy from the official COPR package (confined as httpd_t)
# ---------------------------------------------------------------------------
log "Caddy"
if ! rpm -q caddy >/dev/null 2>&1; then
    rpm -q dnf-plugins-core >/dev/null 2>&1 || dnf install -y dnf-plugins-core
    if [[ ! -f /etc/yum.repos.d/_copr:copr.fedorainfracloud.org:group_caddy:caddy.repo ]]; then
        dnf copr enable -y @caddy/caddy
    fi
    dnf install -y caddy
fi

install -d -o caddy -g caddy -m 0750 /var/lib/caddy
# Caddy reaches the 0660 sylphd socket through its supplementary groups.
id -nG caddy | tr ' ' '\n' | grep -qx "$SYLPH_USER" ||
    usermod -aG "$SYLPH_USER" caddy

systemctl enable caddy >/dev/null 2>&1

# ---------------------------------------------------------------------------
# 7. sylphd systemd unit and the update script
# ---------------------------------------------------------------------------
log "sylphd units"
install -m 0644 "$HERE/sylphd.service" /etc/systemd/system/sylphd.service
install -m 0755 "$HERE/update.sh" /usr/local/sbin/sylphd-update
# sudo's secure_path on Oracle Linux is /sbin:/bin:/usr/sbin:/usr/bin, so
# expose the script there as well to make `sudo sylphd-update` work as-is.
ln -sfn /usr/local/sbin/sylphd-update /usr/sbin/sylphd-update
systemctl daemon-reload
systemctl enable sylphd.service >/dev/null 2>&1

# ---------------------------------------------------------------------------
# 8. SELinux: label the socket directory and grant httpd_t connectto
# ---------------------------------------------------------------------------
# Caddy is httpd_t.  /srv/sylphd/run is labelled httpd_var_run_t (a type httpd_t
# may read and write), and a small module adds the missing unix_stream_socket
# connectto permissions.  Both are needed; see deploy/README.md.
if [[ $(getenforce 2>/dev/null || echo Disabled) != "Disabled" ]]; then
    log "SELinux"
    rpm -q policycoreutils-python-utils >/dev/null 2>&1 || dnf install -y policycoreutils-python-utils
    rpm -q checkpolicy >/dev/null 2>&1 || dnf install -y checkpolicy

    label_tree() {
        semanage fcontext -a -t "$1" "$2" 2>/dev/null ||
            semanage fcontext -m -t "$1" "$2"
    }
    # systemd starts services from init_t, and the resulting unconfined_service_t
    # process may only entrypoint bin_t/shell_exec_t/usr_t files.  Mark the two
    # trees that hold locally installed software (release venvs, managed
    # CPython) as usr_t; data (db/, run/, cache) keeps its own labels.
    label_tree usr_t "$ROOT/releases(/.*)?"
    label_tree usr_t "$ROOT/python(/.*)?"
    restorecon -RF "$ROOT/releases" "$ROOT/python"

    # Caddy (httpd_t) needs to read/write and connect to the socket.
    label_tree httpd_var_run_t "$ROOT/run(/.*)?"
    restorecon -RF "$ROOT/run"

    if [[ -f $HERE/selinux/sylphd_httpd_uds.te ]]; then
        module_dir=$(mktemp -d)
        trap 'rm -rf "$module_dir"' EXIT
        checkmodule -M -m -o "$module_dir/sylphd_httpd_uds.mod" \
            "$HERE/selinux/sylphd_httpd_uds.te"
        semodule_package -o "$module_dir/sylphd_httpd_uds.pp" \
            -m "$module_dir/sylphd_httpd_uds.mod"
        semodule -i "$module_dir/sylphd_httpd_uds.pp"
        rm -rf "$module_dir"
        trap - EXIT
    fi
else
    log "SELinux is disabled — skipping policy"
fi

# ---------------------------------------------------------------------------
# 9. Caddyfile
# ---------------------------------------------------------------------------
if [[ -f $HERE/Caddyfile ]]; then
    log "Caddyfile"
    if cmp -s "$HERE/Caddyfile" /etc/caddy/Caddyfile; then
        log "Caddyfile unchanged"
    else
        if [[ -f /etc/caddy/Caddyfile ]]; then
            cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.$(date -u +%Y%m%dT%H%M%SZ).bak"
        fi
        install -m 0644 -o root -g root "$HERE/Caddyfile" /etc/caddy/Caddyfile
        /usr/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile ||
            die "Caddyfile is invalid"
        systemctl restart caddy
    fi
fi
systemctl is-active --quiet caddy || systemctl start caddy

log "done — next: sudo sylphd-update${SYLPHD_VERSION:+ $SYLPHD_VERSION}"
