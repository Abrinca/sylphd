# Deployment

Everything needed to run a public sylphd instance on a small Oracle Linux host:
Caddy terminates TLS, sylphd speaks HTTP on a Unix socket, systemd runs it as
an unprivileged service account, and `sylphd-update` installs releases from
GitHub with checksum verification and rollback.

The reference host is Oracle Linux 10.2 (aarch64 or x86_64), SELinux enforcing,
firewalld active, 2 vCPU / ~10 GiB RAM.

## Files

| file | installed to | purpose |
|---|---|---|
| `setup-server.sh` | run from a checkout | idempotent host bootstrap and reconciliation |
| `update.sh` | `/usr/local/sbin/sylphd-update` | install/upgrade a release, switch, health check, rollback |
| `sylphd.service` | `/etc/systemd/system/sylphd.service` | the server unit |
| `uv-self-update.service` | `/etc/systemd/system/` | daily `uv self update` |
| `uv-self-update.timer` | `/etc/systemd/system/` | its timer |
| `Caddyfile` | `/etc/caddy/Caddyfile` | TLS front end |
| `selinux/sylphd_httpd_uds.te` | compiled into a policy module | lets Caddy (`httpd_t`) reach the sylphd socket |

## First-time setup

The database files are not part of this repository. Put them on the host first
(or afterwards) as `/srv/sylphd/db/gtdb-r232-c200-dbv2.syl2db` and
`/srv/sylphd/db/meta-r232-reps.sqlite`, owned by `sylphd:sylphd`.

1. Open TCP 80 and 443 in the cloud firewall / VCN security list. Caddy needs
   them for the ACME HTTP-01 challenge and for HTTPS.
2. Point a DNS name at the host. `140-238-213-53.sslip.io` needs no DNS setup
   (sslip.io resolves the embedded IP). Change the site address in `Caddyfile`
   for your own domain.
3. Copy the `deploy/` directory to the host and run the bootstrap as root:

   ```bash
   rsync -av deploy/ opc@HOST:/tmp/sylphd-deploy/
   ssh opc@HOST 'sudo /tmp/sylphd-deploy/setup-server.sh'
   ```

   The script creates the `sylphd` user and `/srv/sylphd/{db,releases,run,python}`,
   installs uv and a uv-managed CPython 3.14, installs Caddy from the
   `@caddy/caddy` COPR, writes the units and the Caddyfile, applies the SELinux
   labels/policy, opens firewalld services, configures dnf-automatic and enables
   the timers. Re-running it is the supported way to re-apply configuration.

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
curl -s https://HOST/health
curl -s https://HOST/info | jq .limits
sudo systemctl restart sylphd             # ~4 s to load the database
```

Startup log line (in the journal) reports database, genome count, limits and
CORS origins. `/health` and `/info` are never rate limited; the three POST
endpoints share a per-client limit of 10/min and a queue of 16.

Requests reach sylphd through the Unix socket, so the server trusts
`X-Forwarded-For` only from that socket (`--trusted-proxy unix`) and Caddy
replaces the header with the real peer address. The access log in
`journalctl -u caddy` shows `client_ip`.

Caddy rejects bodies over 64 MB (`request_body max_size`); sylphd enforces the
per-endpoint limits itself (20 MB for `/assembly` before decompression, 512 MB
overall). Raise the Caddy limit only if you raise the sylphd limits.

## Rollback

`sylphd-update` rolls back automatically when the new release does not become
healthy. Manual rollback: point `current` at the previous release directory and
restart (see above); all releases that were not pruned are still installed.

## Changing the domain

1. Edit `/etc/caddy/Caddyfile` (or `deploy/Caddyfile` and re-run
   `setup-server.sh`): replace `140-238-213-53.sslip.io` with the new name.
2. `sudo systemctl reload caddy` (or restart; Caddy obtains a new certificate
   automatically, keeping the old one until it expires).
3. Update `--cors-origin` in `deploy/sylphd.service` if the web page moves, and
   `--terms-url` if needed, then re-run `setup-server.sh` and restart sylphd.
4. Update `web/config.js` (`apiBase`) to the new URL and publish the page.

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
  another name for a service that loads arbitrary Python and a 20 GB mmapped
  database — not worth the policy surface.
- `/srv/sylphd/releases` and `/srv/sylphd/python` are labelled `usr_t`: systemd
  starts services from `init_t`, and `unconfined_service_t` may only execute
  (`entrypoint`) `bin_t`, `shell_exec_t` and `usr_t` files. Without this the
  unit fails with `status=203/EXEC`.
- Verify with `ausearch -m avc -ts recent` after a deploy; the expected state
  is no AVC records.
