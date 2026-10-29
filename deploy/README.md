# Self-hosting sylphd

Everything needed to run a public sylphd instance on a small Linux host: Caddy
terminates TLS, sylphd speaks HTTP on a Unix socket, systemd runs it as an
unprivileged service account, and `sylphd-update` installs releases from GitHub
with checksum verification and rollback.

The reference platform is Oracle Linux 10 (aarch64 or x86_64) with SELinux
enforcing and firewalld active, on 2 vCPU / 10 GiB; much less works, since the
server only reads a database and runs a handful of profiling jobs. Nothing in
the scripts is Oracle-specific — any systemd distribution with the `@caddy/caddy`
COPR available will do, and the SELinux steps are simply skipped when SELinux
is off.

## Files

| file | installed to | purpose |
|---|---|---|
| `setup-server.sh` | run from a checkout | idempotent host bootstrap and reconciliation |
| `site.env.example` | copied to `site.env` (git-ignored) | your domain and site policy |
| `update.sh` | `/usr/local/sbin/sylphd-update` | install/upgrade a release, switch, health check, rollback |
| `sylphd.service` | `/etc/systemd/system/sylphd.service` | the server unit |
| `uv-self-update.service` | `/etc/systemd/system/` | daily `uv self update` |
| `uv-self-update.timer` | `/etc/systemd/system/` | its timer |
| `Caddyfile` | `/etc/caddy/Caddyfile` | TLS front end (site address from `$SYLPHD_DOMAIN`) |
| `selinux/sylphd_httpd_uds.te` | compiled into a policy module | lets Caddy (`httpd_t`) reach the sylphd socket |

## What is site policy and what is not

The scripts are the mechanism; these are the knobs you choose:

- **Domain** — `DOMAIN` in `site.env`. `setup-server.sh` writes it into a
  `caddy.service` drop-in and Caddy requests a certificate for it.
- **Public flags** — `SYLPH_SERVE_ARGS` in `site.env`, on top of the defaults
  in `sylphd.service`. The shipped set is an example, not a requirement:
  `--allow-sequences` (needed by the web app), `--max-jobs`, `--max-queue`,
  `--rate-limit`, `--trusted-proxy unix`, `--cors-origin`, `--terms-url`.
  `--about /etc/sylphd/about.md` publishes a Markdown description of your
  instance (shown at the top of the web page, editable without a restart;
  put the file somewhere the `sylphd` service account can read, for example
  `/etc/sylphd/about.md`) and `--name` a display name. `SYLPH_SERVE_ARGS` is
  split on whitespace, so use a one-word `--name` there and put anything longer
  in the about file.
  Narrow `--cors-origin` to your page's origin if you run one; drop
  `--terms-url` unless you publish terms. See
  [Running a public server](../docs/guides/public-server.md) and the
  [security notes](../docs/reference/security.md).
- **Upload ceiling** — `SYLPH_HTTP_MAX` (Caddy `request_body max_size`).
- **Allowed ports** — TCP 80 and 443 for ACME and HTTPS, 22 for administration.

`site.env` names a real host and possibly real policy, so it is git-ignored.
Keep a copy wherever you keep operational notes.

## First-time setup

The database files are not part of this repository. Put them on the host first
(or afterwards) under `/srv/sylphd/db/`, named as `SYLPH_DB` / `SYLPH_META`
say (`gtdb-r232-c200-dbv2.syl2db` and `meta-r232-reps.sqlite` by default),
owned by `sylphd:sylphd`.

1. Open TCP 80 and 443 in the cloud firewall / security list. Caddy needs them
   for the ACME HTTP-01 challenge and for HTTPS.
2. Point a DNS name at the host. Without DNS, a name like
   `203-0-113-7.sslip.io` resolves to the embedded IP and gets a certificate
   the same way.
3. Copy `deploy/` to the host, create `site.env`, and run the bootstrap as root:

   ```bash
   rsync -av deploy/ opc@HOST:/tmp/sylphd-deploy/
   ssh opc@HOST 'cd /tmp/sylphd-deploy && sudo DOMAIN=sylphd.example.org ./setup-server.sh'
   ```

   The script creates the `sylphd` user and `/srv/sylphd/{db,releases,run,python}`,
   installs uv and a uv-managed CPython 3.14, installs Caddy from the
   `@caddy/caddy` COPR, writes the units, the Caddyfile and the site drop-ins,
   applies the SELinux labels/policy, opens the firewalld services, configures
   dnf-automatic and enables the timers. Re-running it is the supported way to
   re-apply configuration.

4. Install the first release:

   ```bash
   ssh opc@HOST 'sudo sylphd-update v0.1.0'
   ```

## Deploying a release

```bash
sudo sylphd-update            # newest GitHub release
sudo sylphd-update v0.2.0     # or a specific tag
```

The script refreshes the managed CPython 3.14 patch release, downloads the
release assets for the machine's architecture, verifies them against
`SHA256SUMS`, creates `/srv/sylphd/releases/<version>-<timestamp>/`, installs
with `uv pip install --find-links <dir>`, smoke-tests `sylphd --version`,
atomically switches the `/srv/sylphd/current` symlink, restarts the unit and
waits for `/health` on the socket. If the health check fails it switches back
and exits non-zero. The newest three releases are kept.

A release is never modified in place, so making a release the live one again is
just a symlink swap:

```bash
sudo ln -sfn /srv/sylphd/releases/v0.1.0-20261028T150431Z /srv/sylphd/current.next
sudo mv -Tf /srv/sylphd/current.next /srv/sylphd/current
sudo systemctl restart sylphd
```

## Operations

```bash
systemctl status sylphd caddy
journalctl -u sylphd -u caddy -f          # both logs, newest last
journalctl -u sylphd --since -15min
curl -s https://DOMAIN/health
curl -s https://DOMAIN/info | jq .limits
sudo systemctl restart sylphd             # a few seconds to load the database
```

The startup log line (in the journal) reports database, genome count, limits
and CORS origins. `/health` and `/info` are never rate limited; the three POST
endpoints share one per-client bucket (`--rate-limit`) and a queue
(`--max-queue`).

Requests reach sylphd through the Unix socket, so the server trusts
`X-Forwarded-For` only from that socket (`--trusted-proxy unix`) and Caddy
replaces the header with the real peer address. The access log in
`journalctl -u caddy` shows `client_ip`.

Caddy rejects bodies over `SYLPH_HTTP_MAX` (64 MB by default); sylphd enforces
the per-endpoint limits itself (20 MB for `/assembly` before decompression,
512 MB overall). Raise the Caddy limit only if you raise the sylphd limits.

Releases roll forward, packages roll on their own: `setup-server.sh` enables
`dnf-automatic` (security and bugfix updates, reboot when the kernel needs it)
and a daily `uv self update` timer.

## Rollback

`sylphd-update` rolls back automatically when the new release does not become
healthy. Manual rollback: point `current` at the previous release directory and
restart (see above); all releases that were not pruned are still installed.

## Changing the domain

1. Edit `DOMAIN` in `site.env` on the host (or pass it as an argument), then
   re-run `sudo ./setup-server.sh`. Caddy is restarted, not reloaded: the
   running process holds the old environment.
2. Caddy obtains a certificate for the new name automatically and keeps the old
   one until it expires.
3. Re-check `--cors-origin` in `SYLPH_SERVE_ARGS` if the web page moves, and
   `--terms-url` if you publish terms. Re-running the script applies them;
   `sudo systemctl restart sylphd` picks them up.
4. Point the published page at the new URL (`apiBase` in `web/config.js`) and
   republish it.

## SELinux notes

The host stays enforcing; no boolean is flipped and no service runs unconfined
by hand.

- Caddy comes from the official COPR and is `httpd_exec_t`, so it runs confined
  as `httpd_t`. The packaged unit is used unchanged.
- sylphd runs as `unconfined_service_t` (a plain Python service without a
  custom policy domain). Its socket is `0660 sylphd:sylphd`; user `caddy` is a
  member of group `sylphd` for the DAC check.
- `/srv/sylphd/run` is labelled `httpd_var_run_t`, the type `httpd_t` may read
  and write.
- An AF_UNIX socket keeps the **creating process's** context as its peer SID,
  so the connect check is `httpd_t` → `unconfined_service_t`, which the shipped
  policy does not allow. `selinux/sylphd_httpd_uds.te` adds exactly two
  `unix_stream_socket connectto` rules (to `httpd_var_run_t` and to
  `unconfined_service_t`). Giving sylphd its own SELinux domain would produce a
  nicer type name, but it means re-creating `unconfined_service_t` under
  another name for a service that loads arbitrary Python and a multi-GB mmapped
  database — not worth the policy surface.
- `/srv/sylphd/releases` and `/srv/sylphd/python` are labelled `usr_t`: systemd
  starts services from `init_t`, and `unconfined_service_t` may only execute
  (`entrypoint`) `bin_t`, `shell_exec_t` and `usr_t` files. Without this the
  unit fails with `status=203/EXEC`.
- uv replaces its own binary through a temp file, which keeps a `tmp_t` label;
  the unit's `ExecStartPost=` (and `setup-server.sh`) run `restorecon` so the
  next start is not denied.
- Verify with `ausearch -m avc -ts recent` after a deploy; the expected state
  is no AVC records.
