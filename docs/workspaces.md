# Workspaces: a customer's Odoo environments created from templates

Design, decided with Matteo on 2026-10-29, one choice at a time. Every
statement marked *verified* was checked as written; *not verified* ones must
be tried before code rests on them. What is implemented is described in the
README (Workspaces): phases 1 (workspaces and their users), 2 (the virtual
disk, the instances from templates) and 3 (nginx and the certificates) are
done; the firewall, copying production to test, the editable Odoo
parameters and the publication of the templates are not yet.

## Goal

For a new customer, installing OmniaCLI is enough: from its web interface an
OmniaSolutions user creates the customer's workspace, and the customer's
production and test Odoo instances are created from a template prepared by
OmniaSolutions, each reachable on its own domain over HTTPS.

## Two classes of users

| Class | Who | Sees and does |
|---|---|---|
| 1 | today's users (roles `user`, `developer`, `admin`) | everything, as today; nothing changes for them |
| 2 | workspace users | only the workspaces a class-1 user assigns them, inside a closed sandbox |

A class-2 user has a role **per workspace** (`user`, `developer` or `admin`,
with today's meanings), e.g. `admin` of workspace Acme and `developer` of
workspace Beta.

| Role in a workspace | May, in that workspace only |
|---|---|
| `user` | see instances, logs, jobs and backups; restart, start and stop |
| `developer` | also: pull, module update, backup and its download, the container shell, psql |
| `admin` | also: create and delete instances from templates, the customer's git repositories, the editable Odoo parameters and the debug log switch, the subdomains, the users of the workspace |

Class 1 only: creating a workspace, its limits, its domain, its storage and
its first admin; Plugins, Options, OmniaCLI's own log and restart, Discover,
links, pullers, tunnels, the host shell, Python scripts in schedules.

## What a workspace user can change

- **Instances** only from templates (Odoo now, the API server later).
- **Custom modules** only from git repositories over `https://`, submodules
  included. OmniaCLI clones them into the workspace and adds them to
  `addons_path` in the Odoo configuration it writes. *Verified:* a clone or
  pull from a remote brings neither hooks nor `.git/config` with it.
- **Odoo parameters** from a list with bounds (below), and a debug log switch.
- **Shells**: the shell of a container of the workspace and psql, as the
  image's user (`odoo`, `postgres`), never root. They stay inside the
  container.

Why the repositories are written by OmniaCLI only and mounted read-only:
*verified* (git 2.30.2), `git pull` in a repository whose `.git` someone else
can write runs that person's `core.fsmonitor` and hooks as the user running
git. OmniaCLI pulls on the host, so a customer who could write `.git` would
run code as OmniaCLI's user.

## On disk

```
<storage>/.disks/<workspace>.img                 # the workspace's virtual disk
<storage>/<workspace>/                           # where it is mounted
<storage>/<workspace>/<instance>/production/     # compose project
<storage>/<workspace>/<instance>/production/data/filestore
<storage>/<workspace>/<instance>/production/data/pgdata
<storage>/<workspace>/<instance>/test/
<storage>/<workspace>/<instance>/addons/<repo>/  # git https, written by OmniaCLI, mounted :ro
```

- The storage roots are listed by root in `/etc/omniacli/omniacli.yaml`
  (`workspaces.storage`); a class-1 user picks one for each workspace, so
  workspaces may live on different disks. No symbolic links: the compose
  helper refuses a project reached through one (`composehelper.py`).
- Everything is root's. Names are `[a-z0-9-]`.
- Filestore and database are bind mounts in the instance's folder, not named
  volumes, which would land in docker's data root on another disk.
  *Verified* (odoo:20.0, postgres:17): postgres takes its `pgdata` folder by
  itself (uid 999); Odoo (uid 100:101) fails with `PermissionError` on
  `/var/lib/odoo/filestore` unless the helper gives that folder to 100:101
  first.

### Disk limit: a virtual disk per workspace

- An ext4 image file, mounted on `<storage>/<workspace>`. Full disk: only
  that workspace stops. It grows as it is used (sparse): the limits of the
  workspaces may add up to more than the real disk, so the Disks page shows,
  per storage root, the limits promised next to the real free space, and
  warns past a threshold. It can be enlarged while mounted (`resize2fs`).
- Mounted by a **systemd mount unit** the root helper writes, not by
  OmniaCLI: restarting, upgrading or crashing OmniaCLI never touches a disk
  or a container, and after a reboot everything starts without OmniaCLI.

  ```ini
  # /etc/systemd/system/<systemd-escape --path of the mount point>.mount
  [Unit]
  Description=OmniaCLI workspace acme
  Before=docker.service
  [Mount]
  What=/srv/omniacli/.disks/acme.img
  Where=/srv/omniacli/acme
  Type=ext4
  Options=loop
  [Install]
  WantedBy=multi-user.target docker.service
  ```

- Docker starts after the disks: a drop-in
  `/etc/systemd/system/docker.service.d/omniacli-workspaces.conf` with
  `After=` on them. Not `RequiresMountsFor=`: one disk that fails to mount
  would keep docker, and every workspace, from starting.
- The empty mount point is made immutable (`chattr +i`): when a disk is not
  mounted, its containers cannot write in the folder beneath it. The helper
  also refuses any action on a project whose disk is not mounted.
- Templates keep `restart: unless-stopped`: docker restarts the containers
  itself, and an instance stopped by its users stays stopped.
- Outside the workspace limit: docker images (shared) and container logs
  (limited by the template, below).
- *Not verified* (needs root): a loop mount unit at boot with systemd 247,
  the ordering with docker, `chattr +i` on the mount point, online growth,
  PostgreSQL and Odoo on the mounted disk, a full disk.

## Templates

- Downloaded signed from the download server, like the plugins: an index and
  each template's archive, each with its signed manifest (`.json`,
  `.json.sig`), checked with `plugins.public_key`. A base template also ships
  in the package. Whoever controls the download server can withhold a
  template, not hand out one of their own.
- Built from DockerOdooPLM, one per branch (`18.0`, `19.0`, `20.0`).
- Their sources live in this repository, `templates/<name>/`; the base
  template of the package is taken from there.
- Published by `Setup/release.py` of OmniaConfigurator3, a target
  `templates` (`release.py --only templates`): it takes the templates from
  the chosen OmniaCli commit, checks them with the same compose rules as the
  helper, signs them, merges the index `omniacli-templates.json` with the
  published one and uploads them, as it already does for the OmniaCLI
  plugins (index merge, 503 retries, HEAD checks).

An archive holds:

```
template.json     # what the template is and what it asks
compose.yaml      # the project: only ${...} variables, which OmniaCLI writes in .env
odoo.conf.in      # the Odoo configuration, with placeholders OmniaCLI fills
```

```json
{
  "name": "odooplm-20", "label": "OdooPLM 20 (Odoo 20 + PostgreSQL 17)", "version": "2026.1.1",
  "type": "docker", "odoo_service": "odoo", "db_service": "db",
  "web_port": 8069, "websocket_port": 8072,
  "data": [{"path": "data/filestore", "owner": "100:101"}, {"path": "data/pgdata"}],
  "fields": [
    {"name": "IMAGE", "label": "Immagine", "choices": ["ghcr.io/omniagit/odooplm:20.0", "ghcr.io/omniagit/odooplm:20.0-slim"]},
    {"name": "INIT_MODULES", "label": "Moduli iniziali", "pattern": "^[a-z0-9_,]*$", "default": "plm"},
    {"name": "INIT_LANG", "label": "Lingua", "choices": ["", "it_IT", "en_US"]}
  ]
}
```

- The form asks only the template's `fields`, each checked by `choices` or
  `pattern`; their values go only into `.env`, never into `compose.yaml`.
- OmniaCLI chooses: the ports (on `127.0.0.1`), a random database password
  per instance, the compose project name, the paths.
- OmniaCLI writes the Odoo configuration from `odoo.conf.in`: `addons_path`
  (the template's modules and one folder per customer repository),
  `proxy_mode = True`, `http_interface = 0.0.0.0` (*verified*: a
  configuration that replaces the image's without it makes Odoo listen on
  the container's 127.0.0.1 only), `dbfilter` fixed on the instance's
  database, `list_db = False`.
- A compose file is refused, when a template is published and again by the
  helper, if it publishes a port not bound to `127.0.0.1`, uses
  `privileged`, `cap_add`, `network_mode: host` or `pid: host`, mounts
  anything outside the instance's folder or the docker socket, or mounts
  addons or configuration without `:ro`.
- Container logs are limited (`logging: {driver: json-file, options:
  {max-size: "50m", max-file: "5"}}`, *not verified*).
- The DockerOdooPLM image runs Odoo as 100:101 and reads
  `/etc/odoo/odoo.conf` as the official image does (*verified* in the root
  test of phase 2). The configuration is root's, `0644`; the `.env`, with
  the database password, `0600`.

### Editable Odoo parameters

The configuration stays generated by OmniaCLI. The template lists the
parameters a workspace admin may change, with type and bounds; a class-1
user may widen or narrow the bounds for one workspace. A change rewrites the
configuration and restarts the instance.

| Editable | Locked (OmniaCLI needs them) |
|---|---|
| `workers`, `max_cron_threads` | `addons_path`, `data_dir` |
| `limit_time_cpu`, `limit_time_real`, `limit_time_real_cron`, `limit_time_worker_cron` | `db_host`, `db_port`, `db_user`, `db_password`, `db_name`, `dbfilter`, `list_db` |
| `limit_memory_soft`, `limit_memory_hard` (and `_gevent`) | `http_interface`, `http_port`, `gevent_port`, `proxy_mode` |
| `limit_request`, `db_maxconn` | `logfile` (the Log page reads docker's output) |
| `smtp_*`, `email_from` | `admin_passwd` |

- No free `log_level`: the console already filters the levels. A switch
  "Log di debug: sì / no" sets `log_level = debug` or leaves Odoo's default.
- *Verified* in Odoo 20 (`odoo/service/server.py`): `limit_time_cpu` applies
  only with `workers > 0` (`RLIMIT_CPU`, class `Worker`); with `workers = 0`
  a request over `limit_time_real` only logs a warning and heads the server
  to a restart. The page says so when `workers = 0`. The option names exist
  in Odoo 18, 19 and 20 (`tools/config.py`); the threaded behaviour of 18 and
  19 is *not verified*.
- `workers × limit_memory_hard` may not exceed the container's memory limit:
  otherwise the kernel kills Odoo instead of Odoo stopping the one request.

## Creating an instance

A job, for production and test together:

1. the helper checks the template's signature and its compose rules, creates
   the folders, gives the filestore folder to the image's user, writes
   `compose.yaml`, `.env` and the Odoo configuration;
2. the database: `odoo -d <db> -i <modules> --stop-after-init` in a
   container of its own (`run --rm`), with `ODOOPLM_AUTO_INIT=0` (*verified*
   on odoo:20.0); test is created the same way, empty (copying production to
   test is its own action);
3. start, then nginx and the certificate.

The new database keeps Odoo's user `admin` with password `admin` (Odoo 20
`base/data/res_users_data.xml`). The end of the job says so and strongly
advises changing it at the first login: until then whoever reaches the
domain can log in as admin.

## Copy production to test

The test database is dropped and replaced by production's, neutralized (no
scheduled actions, no outgoing mail) and with a new `database.uuid`; then the
changed modules are updated and test is started. The dialog asks how test
gets its filestore:

- **shared with production through hard links**, the default (as for the
  `odoo` type since OmniaCli `29e389c`): production and test of an instance
  are on the workspace's virtual disk, one filesystem, so the links are
  always possible and the files count once against the workspace's limit.
  Odoo never rewrites a filestore file, it only writes new ones and unlinks
  (`omniacli/odoo/link_filestore.py`): what test writes or deletes does not
  touch production's files. The files belong to the image's user in both;
  the root helper makes the links;
- **a physical copy** of production's files.

*Not verified:* copying the database alone on Odoo 18, whose `odoo db dump`
refuses `--no-filestore`. To try on 18, 19 and 20: `pg_dump` in production's
database container into `psql` in test's, through the helper, then `odoo
neutralize` (`odoo/cli/neutralize.py` exists in Odoo 18).

## Network

- Ports published on `127.0.0.1` only. *Verified* from a container on this
  machine: the host's `127.0.0.1` ports (OmniaCLI 8090, PostgreSQL 5432) are
  not reachable, its `0.0.0.0` ports (SSH 22, an Odoo on 8069) and the
  internet are. Each compose project has its own network; the databases are
  not published.
- An egress firewall (`DOCKER-USER`) keeps the workspaces' containers from
  reaching the host and the LAN; only the internet stays (pip, git https).
  *Not verified.*

### Domains and nginx

| Name | Goes to |
|---|---|
| `https://<domain>`, `https://test.<domain>` | production and test of the workspace's **main instance**, chosen by its admins |
| `https://<instance>.<domain>`, `https://<instance>.test.<domain>` | production and test of every instance |
| `https://<sub>.<domain>`, `https://<sub>.test.<domain>` | optional subdomains, each given to an instance (later also a service of the workspace, the API container) |

- The domain is a field of the workspace, set by class 1; a domain belongs
  to one workspace, and is not under another workspace's. A subdomain is one
  label `[a-z0-9-]`, never `test` nor an instance's name; an instance is
  never named `test` or as a subdomain.
- HTTPS on 443; port 80 only redirects and answers the certificate
  challenge. `/websocket` goes to the http port when `workers` is 0 (the
  template's), to the websocket port otherwise (*verified*: 101 through
  nginx 1.18 to Odoo 20 threaded; the `workers > 0` case *not verified*).
- The helper (`web`) writes one file per workspace,
  `/etc/nginx/conf.d/omniacli-<workspace>.conf`, from validated fields and
  the ports it reads from the instances' `.env`; it refuses a name another
  site of nginx has (`nginx -T`, wildcards too), runs `nginx -t`, puts the
  previous file back when the test fails, then reloads (a stopped nginx is
  not started). nginx's default `server_names_hash_bucket_size` (64) does
  not hold these names: the helper sets 256 in `conf.d/omniacli.conf` once,
  unless the machine's nginx sets it.
- Certificates, one per workspace for all its names: Let's Encrypt (certbot
  `--webroot --cert-name omniacli-<workspace>`, renewed by certbot's timer:
  the names must already point to the machine and port 80 be reachable from
  the internet; *not verified* here, no certbot and no public DNS), or the
  customer's own (`owncert`, PEM on the helper's standard input, checked
  with openssl, kept root-only in `/etc/omniacli/certs/<workspace>/`).
  Let's Encrypt no longer mails about expiry (ended 2025-06-04): the
  account's email (`workspaces.acme_email`) is optional.

## Root

- A new root command, `omniacli workspace-helper`, with a fixed list of
  actions: `disk`, `create`, `remove`, `web`, `owncert` (and `firewall`, to come). It
  refuses any path outside the storage roots, symbolic links, an existing
  folder for `create`, and a template that does not verify with the key it
  reads from the main configuration.
- The compose helper does the actions on the projects as today, limited to
  the storage roots.
- The sudoers lines are written by `postinst` when `/etc/default/omniacli`
  has `OMNIACLI_WORKSPACES=1` (then `sudo dpkg-reconfigure omniacli`), as for
  `OMNIACLI_USER`; without it nothing is granted:

  ```
  omniacli ALL=(root) NOPASSWD: /opt/omniacli/venv/bin/omniacli workspace-helper *
  omniacli ALL=(root) NOPASSWD: /opt/omniacli/venv/bin/omniacli compose-helper /srv/omniacli/* *
  ```

  One `compose-helper` line per storage root of the main configuration, so
  a project outside them still needs its own line, as today. In sudoers
  arguments `*` matches anything, spaces and `/` included (*verified* in
  `man 5 sudoers`): the helpers are the only boundary, as the compose helper
  already is.

## Later

- The API server container (no image exists yet).
- The tries that need root: egress firewall, log rotation, virtual disks,
  Copy production to test.
