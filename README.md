# OmniaCLI

<p align="center"><img src="omniacli/static/login-background.jpg" width="320" alt="OmniaCLI"></p>

Manage many Odoo instances from a single web interface.

OmniaCLI is a small web service, behind a login page, that deploys and
operates Odoo instances from git. Each **server** has a **production** and/or
a **test** instance. An instance has one or more git working copies, a
database, an Odoo config file, a log file and a systemd service, an init.d
script, or an Odoo that OmniaCLI starts itself.

- **Dashboard**: every server with the state of its Odoo, Start / Stop /
  Restart, its CPU, and the CPU, network and disks of the machine, with the
  day each disk will be full.
- **Deploy**: git pull (submodules included), update of the changed or chosen
  modules, restart; auto update in a time window; merge of what was tested
  into production, after a backup.
- **Backups** of database and filestore, to download; **copy of production
  into test**, neutralized.
- **Schedules**: at the times of a crontab expression, a git pull, a copy
  of a linked instance's database and a Python script on an instance.
- **Jobs** shown live; an administrator stops one that hangs.
- **Logs** in the browser, with an "errors only" filter.
- **Links**: operate the OmniaCLI of other machines from your own.
- **Shell** of the server in the browser, and **Claude Code** opened in an
  instance's folder.
- **MCP**: AI clients (Claude Code and others) read, update test instances
  and back them up through a token.
- Users with roles (user, developer, admin), an access log, and a Help page.

## Quick start

On Debian 11–13 or Ubuntu 22.04/24.04 (amd64):

```bash
wget https://github.com/OmniaGit/OmniaCLI/releases/latest/download/OmniaCLI.deb
sudo apt install ./OmniaCLI.deb
sudo cat /etc/omniacli/initial-admin-password     # the password of user admin
```

OmniaCLI listens on `127.0.0.1:8090`: put an HTTPS reverse proxy in front of
it (see [Login and security](#login-and-security)), open it, log in as
`admin`, and add your servers on the **Servers** page.

## Screenshots

The screenshots show demo data.

**Login**

![The login page](docs/images/login.jpg)

**Dashboard**: the servers with the state of their Odoo and their CPU, the
whole machine (CPU, network, disks) and the last jobs, updated as they run.
Every disk is recorded once a day (kept three years, in
`<data>/disk_history.json`): its panel row says when it will be full, from the
trend of the last 90 days (at least 7 days of data), and **Disks** (`/disks`)
charts each disk's used space with that forecast.

![The Dashboard](docs/images/dashboard.png)

**Disks**: each disk's used space by day, with the dashed forecast up to the
day it is full.

![The Disks page](docs/images/disks.png)

**A server**: production and test side by side, with their actions, load and
live log; *Merge test to production* and *Copy production to test* at the top.

![The page of a server](docs/images/server.png)

**Update modules**: the changed modules or chosen ones. The last choices of
modules of the instance come back as buttons (*Recent*).

![The Update modules dialog](docs/images/update-dialog.png)

**Copy production to test**: a new backup of production, or one already made.

![The Copy production to test dialog](docs/images/copy-dialog.png)

**Copy from link**: a backup made on a linked instance (a new one or one
already there), transferred here and restored; the job page follows each
step, the transfer with its size, speed and time left.

![The Copy from link dialog](docs/images/copy-from-link.png)

![A Copy from link job during the transfer](docs/images/copy-from-link-job.png)

**A job**, and the **Servers** page where servers are added, tested and
activated.

![A job](docs/images/job.png)

![The Servers page](docs/images/servers.png)

**A job stopped by an administrator**: a module update that never ended,
stopped with *Stop job*.

![A stopped job](docs/images/job-stopped.png)

## What it does

| Action | Production | Test | What runs |
|---|---|---|---|
| Git pull | ✓ | ✓ | `git pull --ff-only <remote> <branch>` in every repository, then `git submodule update --init --recursive` in those with submodules |
| Git pull + restart | ✓ | ✓ | pull, `click-odoo-update` (only the changed modules), restart |
| Update modules | ✓ | ✓ | a popup asks: the changed modules (`click-odoo-update`) or chosen ones (`odoo-bin -u … --stop-after-init --workers=0`), and whether to restart after. The last 5 choices of modules of each instance are offered again; they are kept in the browser (`localStorage`), so each browser has its own |
| Restart | ✓ | ✓ | the instance's systemd or init.d restart, or OmniaCLI's own start of `odoo-bin` (see below) |
| Start, Stop | ✓ | ✓ | `systemctl start`/`stop` of a systemd service (each needs its sudoers line, the **Start/Stop permission** check prints them), or OmniaCLI's own Odoo; a stopped Odoo stays stopped for the auto update |
| Backup | ✓ | ✓ | `click-odoo-backupdb` → `<backup_dir>/<server>/<role>/<db>_<timestamp>.zip`, database and filestore; with **Without filestore** checked, the database only (`--no-filestore`), `<db>_<timestamp>_nofilestore.zip`. The backup before a merge always has the filestore |
| pip requirements | ✓ | ✓ | `<venv_bin>/python -m pip install -r` the `requirements.txt` next to `odoo-bin` and that of every repository having one, in one call |
| pip click-odoo | ✓ | ✓ | `<venv_bin>/python -m pip install click-odoo-contrib`, the scripts OmniaCLI runs in the venv |
| Merge test to production | | ✓ | backup production, merge, push, update modules, restart production |
| Copy production to test | ✓ → | ✓ | a popup asks which backup of production: a new one (with the filestore; without it, test has no attachments; or **shared with production**, see below) or one already made; then stop test, `click-odoo-restoredb --copy --force --neutralize` into the test database (drops it and its filestore first; `--neutralize` from Odoo 16: no scheduled actions, no outgoing mail), `click-odoo-update`, start test. **Filestore shared with production**: the backup is made without filestore, and after the restore test's filestore (`<data_dir>/filestore/<db>`) is a tree of hard links to production's files, made by the test's venv python as its `run_as` user: no extra disk space, test reads every attachment, and what test writes or deletes does not touch production's files (Odoo never rewrites a filestore file, it only writes new ones and unlinks). Refused, before anything is touched, when an Odoo config sets no `data_dir` or the two filestores are not on the same filesystem, or the test user may not link production's files (`fs.protected_hardlinks`). Refused when production and test run different Odoo versions, and on Odoo 20+ (click-odoo-restoredb refuses it). A systemd test needs its Start/Stop sudoers lines; an init.d one is not stopped, it is restarted at the end |
| Copy from link | ✓ (admin) | ✓ | for an instance with a **Database from link**: a popup asks which backup of that instance of the linked OmniaCLI: a new one, made there now (with or without the filestore; an OmniaCLI there older than 2026.1.12 makes it with the filestore), or one already there; whether to neutralize; whether to set every user's password to `admin` (on by default: after the restore `click-odoo` runs `odoo/reset_passwords.py`, which writes the pbkdf2_sha512 hash in SQL, so no password policy refuses it; in `password` from Odoo 12, in `password_crypt` on Odoo 10 and 11 with auth_crypt; refused before anything is touched when the Odoo version is unknown); whether to **only load the database** (off by default, for development: the instance is not stopped, its modules are not updated and it is not restarted; Odoo's drop of the database ends its connections); and for a production its database name, typed. The zip is downloaded into this instance's backups as `<link>_<file>` (through `<file>.part`: a cut download leaves nothing), then restored as above: stop, `click-odoo-restoredb --copy --force [--neutralize]`, the passwords, `click-odoo-update`, start. Refused, before anything is touched, when the backup's `manifest.json` names another Odoo version than the instance's |
| Log | ✓ | ✓ | `tail -F` of the Odoo log in the browser, with an "errors only" filter |

The two pip buttons are also next to the **Venv bin** field of the server
form (admins), as soon as it is set: they run on the venv, odoo-bin and
repository folders typed there, saved or not, so a draft's venv can be
prepared before Test & activate. The job's log opens in a new tab.

- **Dashboard**: one row per live server, with the CPU of production and
  test over the last 15 minutes. The server's name opens its page, for the
  maintenance: CPU and memory charts of the last hour, the actions, the
  settings and the live log. *Merge test to production* is on that page. The
  buttons **Production** and **Test** open the instance itself in a new tab,
  at its **Domain name** (server form) or at `http://localhost:<its port>`;
  the same link is next to the instance's name on the server's page.
- **Load**: every 5 seconds OmniaCLI samples each live instance. A systemd
  instance is measured on its unit's cgroup (cgroup v2): CPU of the main
  process and every worker, memory as the resident size of its processes. An
  init.d instance is measured on the processes started with `-c <its Odoo
  config>`. CPU is in percent of one core.
- **Test** (server form): runs the same checks on what the form holds, saves
  nothing and leaves the form open, to fix and test again. **Branches** (a
  repository row) reads the remote's branches (`git ls-remote --heads`) and
  turns the Branch cell into a list of them. The Servers page (`/config`)
  has a search field: every word typed, in a server's name, status,
  database, repository names or branches.
- **Discover** (Servers page, admins): for a machine where OmniaCLI is new.
  It finds the Odoo instances there, from the processes running (an
  `odoo-bin`, `odoo` or `openerp-server` started by a python) and from the
  systemd units that start one, stopped ones too. For each it shows what it
  read and fills in the instance:
  - the Odoo config (`-c`) and, from it, the database, the log file and the
    git repositories of the `addons_path`, each with its branch and remote;
  - what the command line sets wins over the config: `-d`, `--logfile`,
    `--http-port`, `--addons-path`;
  - odoo-bin, also when it was started as a bare `odoo-bin` from its
    folder: through the process's working directory, the unit's
    `WorkingDirectory`, or the repositories. It is checked: the file exists
    and `odoo/release.py` next to it gives the Odoo version. Its repository
    is added when the `addons_path` does not name it;
  - the venv's bin folder (the python of the command; a python outside a
    venv is noted);
  - the user it runs as, as `run_as` when it is not OmniaCLI's own;
  - the service: the unit the process runs in, only when that unit's
    `MainPID` is the process (an Odoo started by hand from a shell of
    another service runs inside that service's unit). Started by hand, it is
    noted, with restart type `process` proposed when it runs as OmniaCLI's
    user.

  What it could not find is listed as a note to complete by hand (a config
  OmniaCLI's user cannot read, with the `adduser` that fixes it). Odoo in a
  container, Odoo's gevent server and its workers are skipped; an instance
  OmniaCLI already has is marked. Each one chosen gets a server name and a
  role, and is saved as a **draft**: two with the same name are the
  production and the test of one server. Nothing on the machine is changed.
- **Draft and Test & activate**: a new or cloned server is a draft (no page,
  no actions, no auto update). *Test & activate* runs real checks as the
  OmniaCLI user and makes it live only when no check fails: Odoo config,
  addons path, log file, click-odoo scripts and an `import click_odoo, odoo`
  with the instance's python, the database (connected with the Odoo config's
  credentials), odoo-bin, every repository (branch, no local change, remote
  reachable), the service and `sudo -n -l` for its restart command, separate
  folders and databases for production and test, the backup folder. Saving
  a live server runs the same checks: if they fail nothing is saved and it
  stays live. `omniacli check-config` runs them from the command line.
- **Shell** (developers and admins, a button on each instance): a page with
  a terminal (xterm.js) running a login shell of the user OmniaCLI runs as
  (`omniacli`, or the one of `OMNIACLI_USER`; bash when its own shell is
  `nologin`), in the instance's first repository. It runs on a
  pseudo-terminal joined to the page by a WebSocket, and ends when the page
  closes: its whole session is hung up (SIGHUP, SIGKILL after 3 seconds).
  The shell of a linked instance's server opens through the link, with the
  lower of the link's role and the user's. Every shell opened and closed is
  in the access log, with who and for how long. It is the access of that
  system user, as SSH would give it: give the developer role accordingly.
  **A−** and **A+** change the text size for that page, **Full screen**
  fills the screen with the terminal (Esc leaves it); the size it opens
  with is on the Options page.
- **Options** (admins, `/options`): the options of the whole instance, in
  `options.yaml` next to the servers file (`options_file` in the main
  configuration): the text size every console opens with (8-40 pixels,
  default 14).
- **Consoles**: every log (an instance's, OmniaCLI's own, a job's) and every
  terminal (shell, Claude, psql) is the same widget (`templates/_console.html`)
  with the same bar: A− and A+ for the text size, kept in the browser for
  each kind of console, and Expand, which fills the browser window (Esc
  leaves it in a log; F11 makes the browser full screen). A log also has
  tags, combined: the levels of its records (a traceback goes with its
  record), and for a job its steps, commands and their output; no tag
  chosen shows every line. A text filter and Follow complete it.
- **Tunnels** (every user who logs in, `/tunnels`; the menu names it to
  developers and administrators only): a port of this machine
  that reaches the web interface of an instance of a linked OmniaCLI, for
  when this OmniaCLI reaches it and the browser does not. Every connection
  to the port travels in a WebSocket of its own through the link; the
  other side connects only to that instance's own ports (Odoo: its web
  port, and its gevent port for `/websocket` when it runs workers; before
  Odoo 16 `/longpolling` and `longpolling_port`). The web interface is at
  the root of its own address, as Odoo needs (its pages call root paths:
  they cannot be served under `/links/<name>/`). Options: the address the
  ports listen on (`127.0.0.1` by default: only a browser on this machine
  reaches them; `0.0.0.0` and a firewall rule for a server), the ports
  (`18000-18099`, the first free one is taken) and the hours without
  traffic after which a tunnel closes (4). Who opened a tunnel, or an
  admin, closes it; OmniaCLI stopping closes them all. The port has no
  login of its own: whoever reaches it reaches the instance's login. The
  browser keeps cookies by address, not by port: two tunnels open at once
  share Odoo's session. An admin logging in to Odoo through a tunnel
  makes Odoo set `web.base.url` to the tunnel's address, unless the
  instance has the system parameter `web.base.url.freeze`. Both instances
  need a version with tunnels.
- **Claude** (same roles, a button on an instance with a **Claude folder**
  in the server form): the same shell, opened in that folder, that runs
  `claude` first (found by the login shell's `PATH`; Claude Code's installer
  puts it in `~/.local/bin`) and stays open when it ends. Claude Code must be
  installed and logged in for the user OmniaCLI runs as.
- **psql** (same roles, Odoo instances): the same page running `psql` on
  the instance's database, connected as Odoo connects (`db_host`,
  `db_port`, `db_user`, `db_password` of its Odoo config, given in the
  `PG*` environment, not on the command line); it ends when psql ends.
  `psql` must be installed (Debian: `postgresql-client`). It runs as the
  user OmniaCLI runs as, or with `run_as` as that user (`sudo -n -u <user>
  --preserve-env=PG… psql`), so that an Odoo config without `db_host`
  passes PostgreSQL's `peer` login on the local socket as Odoo does. That
  needs a sudoers line for psql, with `SETENV` for the `PG*` variables;
  the **psql** check (a warning) prints it.
- **Auto update**: per instance, every `interval_minutes` within `window`,
  OmniaCLI fetches every repository. When there are new commits it runs
  *git pull + restart* as an `auto` job.
- **Jobs**: every action is a job. Its output, its status and its error are
  shown live in the web page, and the last 500 are kept in `jobs_dir`.
- Only one job runs on an instance at a time; the others wait.
- **Stop job** (admins, on the page of a queued or running job): for a job
  that hangs or loops. The program it runs gets SIGTERM, with its whole
  process group (pip's builds, Odoo's workers), and SIGKILL after 10 seconds.
  The job ends as `stopped`, "stopped by <user>"; the steps after the stopped
  one do not run (a stopped module update is not followed by the restart),
  and what the job did before stays done. The instance is free for the next
  job at once. With `run_as` the SIGKILL reaches only `sudo`: a program of
  that user that ignores SIGTERM keeps running.

### Rules the actions follow

- A pull is **fast-forward only**. A repository on a branch other than the
  configured one, or with uncommitted changes, stops the job: OmniaCLI never
  creates merge commits or throws away local work on a pull.
- Except on a **test instance without auto update**, where someone may be
  working: its uncommitted changes are kept (a warning in the checks, not an
  error), and a pull goes on unless git would have to overwrite them.
  **Merge test to production** still refuses them: what runs on test must be
  a commit.
- The **submodules** of a repository follow it: after a pull, a merge or the
  rollback of a merge, `git submodule update --init --recursive` checks them
  out at the commits the repository records (a submodule left behind counts
  as an uncommitted change). Local changes inside a submodule make git
  refuse: the job stops, the repository itself already pulled, the changes
  kept. Their remotes must be readable by OmniaCLI's user without a prompt.
- **Merge test to production** merges the commit **checked out on the test
  server**, not the tip of the test branch, so production gets exactly what
  was tested. That commit must already be on the remote. Test and production
  repositories are paired by `name`. If any repository conflicts, the merge
  is aborted, every production repository is reset to the commit it had
  before, and nothing is pushed: the conflict is for a person to resolve.
- If a module update fails, the instance is **not** restarted.

## Install from the Debian package

Supported: Debian 11, 12, 13 and Ubuntu 22.04, 24.04 (amd64). OmniaCLI needs
Python 3.10 or newer: the package uses the system `python3` when it is 3.10+,
otherwise (Debian 11, Python 3.9) a local `python3.10`+ already installed (in
the `PATH` or `/usr/local/bin`, the newest first), otherwise it downloads a
Python 3.12 (python-build-standalone, checked against its sha256) into
`/opt/omniacli/python`.

Every [release](https://github.com/OmniaGit/OmniaCLI/releases) has the
package as `omniacli_<version>_all.deb`, and as `OmniaCLI.deb`, whose link
always gives the latest one.

```bash
sudo apt install ./omniacli_<version>_all.deb
sudo cat /etc/omniacli/initial-admin-password     # password of user admin
```

An upgrade is the same `apt install` of a newer package: the configuration,
the servers, the users, the jobs and the backups stay.

The package:

- creates the system user `omniacli` and runs the service `omniacli` as that
  user, on `127.0.0.1:8090` (change it in `/etc/default/omniacli`);
- runs it as another user when `OMNIACLI_USER` is set in
  `/etc/default/omniacli` (see below);
- builds `/opt/omniacli/venv` at installation with that Python and the
  dependency versions locked for it (`packaging/debian/requirements`).
  **The machine must reach PyPI while installing**, and also `github.com`
  when the Python 3.12 has to be downloaded. After an upgrade or a removal of
  the Python the venv was built on (a distribution upgrade, a local Python
  removed), rebuild it with `sudo dpkg-reconfigure omniacli`;
- creates `/etc/omniacli/omniacli.yaml` once, with a random password for
  `admin`; upgrades never change it;
- keeps the servers file, the jobs and the backups in `/var/lib/omniacli`;
- installs `/etc/sudoers.d/omniacli`, all comments, to be completed;
- on purge, deletes configuration and jobs but keeps the backups.

### First setup on a machine where Odoo already runs

The usual case: Odoo installed as the system user `odoo`, its configs and
logs readable by group `odoo` only, a systemd service per instance. After
`apt install`, as root:

1. **Let OmniaCLI read the Odoo configs and logs.** Add `omniacli` to the
   group `odoo`, but only if that group exists: `adduser` to a missing group
   fails (checked on Debian 11, 12 and Ubuntu 22.04, 24.04). The service gets
   the new group only when it starts again:

   ```bash
   getent group odoo && sudo adduser omniacli odoo
   sudo systemctl restart omniacli
   sudo -u omniacli head -c 1 /etc/odoo/odoo.conf; echo " $?"   # 0: readable
   ```

   Without a group `odoo` (configs owned by another user), give read access
   to each file with an ACL instead (see Configuration cases below).

2. **Let OmniaCLI run the venv's programs as `odoo`** (`run_as: odoo`:
   click-odoo, `odoo-bin -u`, pip, the database checks), in
   `/etc/sudoers.d/omniacli`. Either one line per venv, which is what the
   **Run as** check prints:

   ```
   omniacli ALL=(odoo) NOPASSWD:SETENV: /opt/odoo/venvs/odoo18/bin/
   ```

   or one line for every venv of the machine, including the ones made later:

   ```
   omniacli ALL=(odoo) NOPASSWD:SETENV: ALL
   ```

   The second one gives no more than the first in practice, because the
   venv's `python` runs any code it is given. With either, OmniaCLI acts as
   `odoo`, never as root. A line naming `odoo` can be written before that
   user exists, for example before Odoo is installed: `visudo` accepts it,
   the other sudo rules keep working, and `sudo -u odoo` answers
   `unknown user odoo` until the user is created, then works (checked on
   Debian 11, 12, 13 and Ubuntu 22.04, 24.04, sudo 1.9.5 to 1.9.16).

3. **Let OmniaCLI restart, start and stop each Odoo service**: one exact
   line per service and action, as root. No wildcard: `systemctl restart *`
   would let OmniaCLI restart any service of the machine, `ssh` included.
   The **Restart permission** and **Start/Stop permission** checks print the
   lines missing:

   ```
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password restart odoo-prod
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password start odoo-prod
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password stop odoo-prod
   ```

   Check the file after every change: `sudo visudo -cf /etc/sudoers.d/omniacli`
   (a broken sudoers file can stop every `sudo` of the machine).

4. **Give OmniaCLI the repositories and a git key**: git runs as
   `omniacli`, which must own the repositories and reach their remotes
   without a prompt (see "dubious ownership" and the deploy key under
   Configuration cases below).

5. **Add the servers**: on the web interface, **Servers → Discover** finds
   every Odoo running, and every systemd unit that starts one, and saves
   the chosen ones as drafts. Complete what its notes say, then run
   **Test & activate** on each: its checks say which of the steps above is
   still missing, with the line or the command to add.

### The service as another user (OMNIACLI_USER)

On a development workstation the repositories, the Odoo configs and the logs
are the developer's, often in a folder other users cannot enter: the service
user `omniacli` cannot read them (`cannot read .../odoo.conf: Permission
denied`). Run the whole service as that user instead:

```bash
echo OMNIACLI_USER=mboscolo | sudo tee -a /etc/default/omniacli
sudo dpkg-reconfigure omniacli
```

Everything OmniaCLI does itself then runs as that user, in group `omniacli`:
git (with that user's git configuration and credentials), reading the
configs and logs, its files in `/var/lib/omniacli` (given to that user, and
kept so by upgrades). The sudoers lines name that user
(`mboscolo ALL=(root) NOPASSWD: ...`). Not root. Remove the line and run
`dpkg-reconfigure` again to go back to `omniacli`. On a server keep
`omniacli`, with `run_as` for the programs of an Odoo venv.

### Build and test the package

```bash
packaging/debian/build_deb.sh                  # test build -> ARTEFACTS, omniacli_<year>.1.<n>_all.deb
packaging/debian/build_deb.sh --release        # release build of branch <year> -> ARTEFACTS, OmniaCLI.deb
packaging/debian/build_deb.sh --check DIR      # the working tree, into DIR, no tag
packaging/debian/build_deb.sh --ci VERSION DIR # the checked out commit with VERSION, into DIR (the CI)
packaging/debian/test_deb.sh <deb> [image ...] # install and check it in Docker, systemd included
packaging/debian/make_locks.sh                 # regenerate the dependency locks, with the tests
```

The package is built from a commit (`git archive`), never from uncommitted
files, except with `--check`. Its version is `<year>.1.<n>`. The test and
release builds number `n` one above every `omniacli-v<year>.1.*` tag and every
package already in `ARTEFACTS`, and tag the built commit. `ARTEFACTS` (where
the package goes, `dist/` by default) and `PYTHON` (the Python that builds the
wheel, `python3` by default) come from the environment or from
`packaging/debian/build.local.sh`, a file of the build machine that is not
versioned.

### Releases on GitHub

Publishing a GitHub release builds its package
(`.github/workflows/release.yml`):

1. tag the commit to release with its version, `omniacli-v<year>.1.<n>`, and
   publish a release on that tag;
2. the workflow runs the tests, builds the package with
   `build_deb.sh --ci <version>` and attaches `omniacli_<version>_all.deb`
   and `OmniaCLI.deb` to the release;
3. it then installs the package in Debian 12 and Ubuntu 24.04 containers with
   systemd and checks it end to end (`test_deb.sh`). A failure there marks
   the run as failed: the package is already attached, so delete it from the
   release, or publish a fixed release.

## Install by hand

```bash
python3.11 -m venv /opt/omniacli/venv
/opt/omniacli/venv/bin/pip install git+https://github.com/OmniaGit/OmniaCLI.git
```

The `click-odoo-*` scripts import the instance's own `odoo` package. So
`click-odoo-contrib` must be installed **in the venv that runs each Odoo**,
not in OmniaCLI's venv. `venv_bin` in the config points to it.

```bash
/opt/odoo/venvs/odoo18/bin/pip install click-odoo-contrib
```

`click-odoo-update` keeps a hash of every installed module in the database.
On a database it has never seen, it would update every module. So run this
once per database, when you know the modules are up to date:

```bash
/opt/odoo/venvs/odoo18/bin/click-odoo-update -c odoo.conf -d DB --only-compute-hashes
```

## Configure

The configuration has six files and a folder:

- the **main config** (`omniacli.yaml`): the users set by hand, `backup_dir`,
  `jobs_dir`, `servers_file` and, when not in their default place, the paths
  of the files below (`users_file`, `links_file`, `groups_file`,
  `schedules_file`) and of the scripts folder (`scripts_dir`). It is edited by hand and the
  web interface never writes it.
- the **users file** (`users_file`, by default `users.yaml` next to the
  servers file): the users, links and MCP tokens managed from the web
  interface (**Users** page, admins only), written readable by its owner
  only. Every change applies at once: a changed or deleted password stops
  working at the next request.
- the **links file** (`links_file`, by default `links.yaml` next to the
  servers file): the other OmniaCLI instances this one operates (**Links**
  page, admins only), with their tokens in clear, so written readable by its
  owner only.
- the **groups file** (`groups_file`, by default `groups.yaml` next to the
  servers file): the headings of the Dashboard (**Edit groups**, admins).
- the **schedules file** (`schedules_file`, by default `schedules.yaml` next
  to the servers file): the scheduled actions (**Schedules** page, admins;
  see Scheduled actions).
- the **scripts folder** (`scripts_dir`, by default `scripts` next to the
  servers file, `/var/lib/omniacli/scripts` in the package): the Python
  scripts a scheduled action may run, put there by hand.
- the **servers file** (`servers_file`, see `servers.example.yaml`): the
  servers and their instances. It is edited from the web interface
  (**Servers** page: new, edit, clone, delete), so the user running
  OmniaCLI must be able to write it. Every save is validated like the file at
  startup, the previous version is kept as `<file>.bak`, and the auto update
  loops restart with the new settings. Clone copies a server's configuration
  only (no database, no files). A server with a running job cannot be edited or
  deleted.

```bash
cp config.example.yaml omniacli.yaml
omniacli hash-password            # paste the result under auth.users
omniacli check-config -c omniacli.yaml
```

`check-config` checks that every path is readable, that the click-odoo
scripts exist and that every repository is on its configured branch.

### Roles

Every user has a role:

| Role | May |
|---|---|
| `user` | watch: servers, load, logs, jobs, the backup list; and restart, start and stop Odoo |
| `developer` | also act on the servers: pull, update, backup and the download of a backup, pip, the shell, Claude |
| `admin` | everything: also the configuration pages (servers, users, links, schedules) and the merge of test into production |

```yaml
auth:
  users:
    admin: "pbkdf2_sha256$..."                 # a bare hash: an admin
    anna: {password: "pbkdf2_sha256$...", role: developer}
    luca: {password: "pbkdf2_sha256$...", role: user}
```

The pages show only what the role allows, and the server refuses the rest
(403).

The **Users** page adds users, changes their role, sets a new password and
deletes them. The users of the main config are shown there too but are
changed only in the file: they are the way in that a mistake on the page
cannot lock out. Nobody can change their own role or delete themselves, and
the last admin cannot be demoted or deleted. Every user who logs in with a
password changes it from their own page (their name, top right), unless it is
set in the main config.

The **Help** page (menu Help, developers and admins) explains step by step
how to create a link, how to connect an MCP client and every field of the
server form, with example data only.

The **Access log** (Users page, admins) lists the logins with date, time and
address: a user's login is the login form; a link or an MCP token, which send
their token with every request, log in with their first request after 30
minutes without requests from the same address. Logouts, every wrong password
or token, and the logins refused for too many failures are listed, and so is
every call to `/mcp`, with its JSON-RPC method and tool, and every shell
opened and closed. It is kept in
`access.jsonl` in the logs folder next to `jobs_dir` (`/var/lib/omniacli/logs`
for the package); past 5 MiB it moves to `access.jsonl.1`.

The **Log** page (admins) shows OmniaCLI's own log: the requests it serves,
its calls to the links, its errors, with the follow and errors-only switches
of the instances' log. OmniaCLI writes it to `omniacli.log` in the same logs
folder, besides what it writes to its standard output (the journal): the
package's user `omniacli` may not read the system journal. Past 5 MiB it
moves to `omniacli.log.1`.

### Linked instances

Another OmniaCLI instance (yours, at the customer's) can be let in to operate
this one with a token of its own and a role. Create it on the **Users** page
(the token is shown once), or by hand in `auth.links`:

```bash
omniacli new-link-token omniasolutions --role developer
```

```yaml
auth:
  links:
    omniasolutions: {token: "sha256$...", role: developer}
```

The linking instance sends the token as `Authorization: Bearer <token>`.
It may also send `X-OmniaCLI-Prefix` (the path it serves these pages under,
e.g. `/links/customer`: every link in the pages starts with it) and
`X-OmniaCLI-User` (its user acting through the link: the jobs record
`manual: link omniasolutions (anna)`), and `X-OmniaCLI-Role-Cap` (that
user's role on the linking instance: the link acts with the lower of it and
its own). They are honoured for link tokens only. `GET /api/info` answers
with the version and the caller's role, `GET /api/servers` with the live
servers.

### Operating another instance

On your instance, the **Links** page (admins) adds a link: a name (the
customer's), the other instance's URL, the token it created for you on its
Users page, and a CA file when its certificate is from the customer's own CA.
**Test link** shows its version and the role you get there. Then:

- the Dashboard lists its live servers as `<link> › <server>`, with their
  load, and the filter finds them by link name too. A link that does not
  answer is shown as an error above the list;
- its pages open here under `/links/<link>/`, with a bar saying which
  instance you are on: every action runs there, recorded as
  `manual: link <its name for you> (<your user>)`;
- you act there with the lower of your role here and the link's role there.
  Only users who log in here use links, not tokens;
- an instance here may take its database from an instance there: in the
  server form, **Database from link** names the link, the server there and
  its role. *Copy from link* on the instance's page then backs it up there,
  downloads the zip and restores it here (see the table of actions). It needs
  the developer role there (for the link and for you) and here, the admin
  role for a production here. Stopping the job here does not stop a backup
  already running there. Without a Database from link, or with a link no
  longer on the Links page, the button is shown disabled, and its tooltip
  says what to set.

### Pullers

An OmniaCLI that nobody can reach (a production behind a firewall) can still
be operated through a third OmniaCLI that both sides reach, on a public VPS:
the **relay**. The unreachable one (the **producer**) and the ones that
operate it (the **consumers**) each link out to the relay; nothing connects
to the producer. On the relay a **puller** names the link of the producer
and those of its consumers:

*Not finished*: this version has the relay and the producer's side, the
consumers' side comes next.


- the **Pullers** page (admins) adds one: a name, the producer and the
  consumers, among the incoming links of the Users page (the role **user**
  is enough: a puller's routes check that the link is in it, and that role
  opens no shell, no backup and no action on the relay's own servers), the
  hours its files and reports are kept (24) and its largest file (10240 MiB).
  Saved in `pullers.yaml` next to the servers file;
- the producer asks for the consumers' commands, holding its request open up
  to 25 seconds until one comes, and leaves with every request its report
  (state, disks, load, the progress of its jobs), which every consumer reads
  from the last one it read. Its backups it uploads, for a consumer to
  download;
- a command not taken by the producer in 10 minutes expires; once taken, it
  is gone from the relay. A file is deleted once a consumer says it has it
  whole, and every file and report after the hours kept; at most 20 commands
  wait and 5 files are kept per puller;
- with a **key** that only the producer and the consumers hold, every
  message and file is encrypted and signed (AES-256-GCM): the relay can
  neither read them nor make one up, nor play a command again. Without a key
  they pass in clear, and the relay is trusted. The page shows only what the
  relay knows: when each side was last seen, the commands waiting and the
  files kept.

On the producer, the **Pullers** page publishes an instance (*Published
here*): the link to the relay (Links page), the puller's name, its key (*New
key* makes one: give the same to the consumers) and the commands it takes,
each with its hours. Saved in `publications.yaml` next to the servers file,
readable by its owner only:

- **backup**, **start**, **stop** and **restart** are off until enabled. A
  command sent outside its hours (`HH:MM-HH:MM`, by the producer's clock,
  may cross midnight; empty: always) is refused at once, the answer saying
  its hours: to run it within them, schedule it on the consumer. Saving
  start, stop or restart without a key warns that whoever controls the
  relay can send them;
- every command runs as a job recorded as `puller <name> (<who asked>)`;
  a consumer may stop a job a command started (`stop-job`), and no other;
- a backup is encrypted with the key before it is uploaded (steps Backup,
  Encrypt, Upload on its job page), and stays here too, as any backup;
- every ask carries a report: the instance's state, whether a job runs, its
  type and its backup's options, the commands exposed and their hours, the
  disks (and their daily history when it changed), the CPU and memory
  samples since the report the relay took last (the sampler keeps an hour:
  a longer gap is lost), and the new log lines, steps and result of the
  jobs the commands started. Whatever the relay did not take goes again
  with the next report. While such a job runs it asks every 2 seconds,
  otherwise every 25;
- the commands already received are kept in `publications/` next to
  `jobs_dir`, so a restart does not let one be played again. The page shows
  when the relay was reached last, or why not.

Their state is kept in `pullers/` next to `jobs_dir`
(`/var/lib/omniacli/pullers` in the package). A relay behind nginx needs
`client_max_body_size 0;` (or the largest file) and
`proxy_request_buffering off;` in its `location`, or nginx refuses an upload
over 1 MiB; the 25-second requests stay within its read timeout.

The routes, for links in the puller only (a JSON `{"error": ...}` otherwise):

| Who | Route | What |
|---|---|---|
| any | `GET /api/pullers` | the pullers the link is in, and its part |
| any | `GET /api/pullers/<name>` | last contacts, commands waiting and expired, files |
| producer | `POST /api/pullers/<name>/producer?wait=<s>` | body `{"report": <envelope>}`: keeps the report, answers `{"seq", "commands"}` |
| producer | `PUT /api/pullers/<name>/producer/files/<file>` | the body is the file |
| consumer | `POST /api/pullers/<name>/commands` | body `{"command": <envelope>}`: answers `{"qid", "expires"}` |
| consumer | `GET /api/pullers/<name>/reports?since=<seq>&wait=<s>` | the reports after `since`, with `next`, `first` (the oldest kept), `last` and `reset` |
| consumer | `GET /api/pullers/<name>/files/<file>` | the file |
| consumer | `DELETE /api/pullers/<name>/files/<file>` | the consumer has it whole: it goes |

### Groups

With many servers, **Edit groups** on the Dashboard (admins) puts them
under headings, in the order you choose. A member is `srv` (a server of this
instance), `*` (all of them), `customer/srv` (a server of a link) or
`customer/*` (all the servers of a link); a server shows under the first
group that has it, the rest under "Other servers". The filter finds a server
by its group name too. The groups are this instance's own: a linked
instance's pages do not show them.

The other instance must be reachable from yours: through an HTTPS reverse
proxy, a VPN, or an SSH tunnel (`http://` is accepted for a tunnel or a VPN,
but then the token travels in clear). Both instances need a version with
links (the Test link button says so).

### Scheduled actions

The **Schedules** page (admins) runs steps on an instance at the times of a
crontab expression of 5 fields (`0 2 * * *`: every day at 02:00, this
computer's time; the form shows the next runs). Each step is optional, in
this order:

1. **git pull** of the instance's repositories. Alone it works as the auto
   update: the modules are updated and the instance restarted only when
   something new came. With the copy it does not restart: the restore does.
2. **Copy from link**: a new backup of an instance of a linked OmniaCLI,
   downloaded and restored here as the Copy from link button does, with the
   same choices (without filestore, neutralize, passwords set to `admin`,
   only load the database; a schedule saved before the passwords choice
   existed keeps the passwords). With *only load* the instance is neither
   updated nor restarted, after a pull too. *Backups kept here* (default
   3) counts the zips of that link in the instance's backups, the new one
   included: the older ones are deleted before the new backup starts. The
   backups made there are not deleted (OmniaCLI has no route for that).
3. **Python script**: a `.py` file of the scripts folder
   (`/var/lib/omniacli/scripts` in the package, `scripts_dir` in the main
   configuration), put there by hand; the web interface does not upload
   scripts. It runs with the instance's python (Odoo: its venv's, as its
   *Run as* user, so it can import odoo) in its first repository's folder,
   with its arguments and `OMNIACLI_SERVER`, `OMNIACLI_ROLE`,
   `OMNIACLI_REPOSITORIES` (JSON name: path), and for Odoo
   `OMNIACLI_DATABASE` and `OMNIACLI_ODOO_CONFIG`. With *Run as*, sudo gets
   the variables on its command line: the sudoers line needs `SETENV`. An
   exit code other than 0 fails the job.

*Acting as* names the jobs' author and the user the linked OmniaCLI is told
acts there (empty: `omniacli`), with the developer role at most. A run that
finds a job on its instance is skipped; runs missed while OmniaCLI was
stopped are not made up. **Run now** runs the steps at once, enabled or not.
Each schedule keeps the history of its last runs (30 by default). The
schedules live in `schedules.yaml` next to the servers file
(`schedules_file`), the history in `schedule_history.json` next to the jobs.

### Permissions

The user that runs OmniaCLI needs:

- to **own** the repositories (or have them listed in its git
  `safe.directory`): git refuses to work in a repository owned by another
  user ("dubious ownership"). It also needs git credentials for `fetch`/`push`
  that need no prompt (an SSH key without a passphrase, or a credential
  helper). Git is never allowed to ask.
- read access to the Odoo config and log files.
- a database connection: `click-odoo-*` connect with the settings of the
  instance's Odoo config file.
- **or**, with `run_as: <user>` on the instance (the user of the Odoo
  service, usually `odoo`), the right to run the venv's programs as that
  user: `click-odoo-update`, `click-odoo-backupdb`, `odoo-bin -u`, pip and the
  checks then run as `sudo -n -u <user>`, so the database is reached as that
  user (PostgreSQL `peer` login on the socket included), and so are the
  filestore and the venv. git still runs as OmniaCLI's user. The venv's
  folder in sudoers allows its programs, `SETENV` passes `PYTHONPATH` (the
  folder of `odoo-bin`). These programs start in `/`, not in OmniaCLI's own
  folder (`/var/lib/omniacli`, which that user cannot enter). The **Run as**
  check prints the line:

  ```
  omniacli ALL=(odoo) NOPASSWD:SETENV: /opt/odoo/venvs/odoo18/bin/
  omniacli ALL=(odoo) NOPASSWD:SETENV: /usr/bin/psql
  ```

  The second line is for the psql page only (the **psql** check, a warning).

  A backup made as that user is written in `/var/tmp` first, then moved
  into the backup folder. Not with restart type `process`: OmniaCLI stops
  its own Odoo with a signal, which it may not send to another user's
  process.
- the right to restart the services without a password. With `sudo: true`
  (the default), the command runs as `sudo -n`. sudoers needs the full path
  that `command -v systemctl` prints on that machine. Example
  `/etc/sudoers.d/omniacli`:

```
omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password restart odoo-customer-a
omniacli ALL=(root) NOPASSWD: /etc/init.d/odoo-customer-a-test restart
```

### Configuration cases

Problems met putting OmniaCLI on an existing Odoo 18 server (Odoo as the
systemd service of user `odoo`, instances with `run_as: odoo`), each with
what the check or the job said, the cause and the fix. Paths are examples.

**"Odoo config: Permission denied", "Log file … is not readable".** The
checks run as `omniacli`. The Odoo configs were `640 odoo:odoo` and
`/var/log/odoo` was `750 odoo:odoo`: readable files in a folder `omniacli`
cannot enter are not readable either. Both are readable by group `odoo`, so:

```bash
sudo adduser omniacli odoo
sudo -u omniacli head -c 1 /etc/odoo/odoo.conf; echo " $?"   # 0: readable
sudo systemctl restart omniacli     # the service gets the new group only when it starts
```

`omniacli` then reads everything group `odoo` reads. To give it only these
files, use ACLs instead (`setfacl -m u:omniacli:r <config>`,
`setfacl -m u:omniacli:rx /var/log/odoo` and `setfacl -d -m u:omniacli:r
/var/log/odoo` for the files logrotate creates). An Odoo config left `666`
during tests is readable and writable by every user of the machine (its
database password; an `addons_path` that runs their code as `odoo`): put it
back to `640`.

**"fatal: detected dubious ownership in repository".** The repositories
belonged to `odoo` (`755`), and git runs as `omniacli`. `safe.directory` alone
is not enough when `omniacli` cannot write there: the pull would then fail.
Give OmniaCLI the repositories it manages:

```bash
sudo chown -R omniacli:odoo /opt/odoo/repos/customer
sudo -u omniacli -H git -C /opt/odoo/repos/customer status --short
```

Odoo keeps reading the code (the folders stay `755`). From then on pull as
`omniacli` (`sudo -u omniacli -H git …`), not as `odoo` or root. The first
`git status` after the `chown` takes a while on a big repository (git
refreshes its index): it is not stuck. Run every manual git command on these
repositories as `omniacli` too, `git … remote -v` included: root gets the same
"dubious ownership".

**"remote origin branch 18.0 not reachable: could not read Username for
'https://github.com': terminal prompts disabled".** `omniacli` had no git
credentials, and OmniaCLI never lets git ask for them. A deploy key of
`omniacli` on the GitHub repository, then the remote on SSH:

```bash
sudo -u omniacli -H mkdir -p -m 700 /var/lib/omniacli/.ssh
sudo -u omniacli -H ssh-keygen -t ed25519 -N '' -f /var/lib/omniacli/.ssh/id_ed25519
sudo -u omniacli -H sh -c 'ssh-keyscan github.com >> ~/.ssh/known_hosts'
# add the .pub as a deploy key of the repository on GitHub
sudo -u omniacli -H git -C /opt/odoo/repos/customer remote set-url origin git@github.com:<owner>/<repo>.git
sudo -u omniacli -H git -C /opt/odoo/repos/customer ls-remote origin 18.0
```

A read-only key is enough for the checks and the pulls. *Merge test to
production* pushes, so it needs **Allow write access**; where whoever merges
the code brings it to production on GitHub, keep the key read-only and leave
the merge to them (with a read-only key the button's push fails after the
local merge, which then has to be undone by hand). Production and test clones
of the same GitHub repository share one key.

**"test repositories without a production repository of the same name".**
*Merge test to production* pairs repositories by `name`: give the test
repository the name of its production one (`customer` in both); path and
branch stay different.

**A module update fails with `PermissionError: … 'ezdxf.ini'`.** A program run
as `odoo` started in OmniaCLI's folder, `/var/lib/omniacli` (`750`), and a
library (ezdxf, imported by an Odoo module) looks for its ini file in the
current folder. Since 2026.1.11 programs run as another user start in `/`.
On an older version, `sudo chmod 751 /var/lib/omniacli` works around it; put
it back to `750` after the upgrade.

**A repository stays " M lib/three.js" after `git reset --hard`.** That path
is a git submodule (a library inside an Odoo module), never initialised on
the server: a reset does not touch submodules, and the check counts a
submodule off its commit as an uncommitted change. `git submodule update
--init` fixes it. Since 2026.1.11 OmniaCLI does it after every pull and merge
(see [Rules the actions follow](#rules-the-actions-follow)); the submodules'
remotes must be readable by `omniacli` without a prompt.

**"Failed to restart odoo.service: Interactive authentication required".**
The restart ran `systemctl` without `sudo`: the instance's restart had
`sudo: false`. Turn **Use sudo -n** on in the server form, and allow the
commands in sudoers with the full path `command -v systemctl` prints:

```
omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password restart odoo.service
omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password start odoo.service
omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password stop odoo.service
```

`sudo -u omniacli sudo -n -l /usr/bin/systemctl --no-ask-password restart
odoo.service` prints the command when the line matches. The start and stop
lines are for the Start and Stop buttons and *Copy production to test*;
the **Start/Stop permission** check says which are missing.

**`<string>:38: (ERROR/3) Unexpected indentation` in an update job.** These
are docutils messages about a module's badly indented RST description: the
update goes on. When it fails, the job shows where Odoo stopped
(`Error: while parsing …/data/….xml`), the reason (the traceback) is in the
Odoo log, which the **Log** page shows once `omniacli` can read it.

**A link or an MCP client to an OmniaCLI behind a self-signed certificate.**
The linking OmniaCLI verifies it with the link's **CA file**: the server's
certificate itself (with its IP in `subjectAltName`), copied to the linking
machine and readable by `omniacli` there. Claude Code runs on Node, which
does not use that file: start it with
`NODE_EXTRA_CA_CERTS=<that certificate>`.

## Odoo started by OmniaCLI (restart type `process`)

For an Odoo that has no service, OmniaCLI can start it itself:
`<venv_bin>/python <odoo-bin> -c <odoo_config>`, in its own session. Its
output goes to `<jobs_dir>/../logs/<server>_<role>.log` (the logfile of the
Odoo config is overridden), and that file is the instance's log; its pid is
kept next to it (`.pid`).

- Restart stops OmniaCLI's Odoo (SIGTERM to its process group, SIGKILL after
  60 s) and starts it again. It refuses when another process runs with the
  same Odoo config (started from a console, an IDE, a service): OmniaCLI
  never stops a process it did not start.
- Stop stops it, and it stays stopped until Start (the same button as
  Restart): the auto update still pulls and updates its modules, but does
  not start it again. Stop exists only for this type: a service is stopped
  with its own tools.
- Odoo keeps running when OmniaCLI stops, restarts or is upgraded: the
  package's unit has `KillMode=process`, and OmniaCLI finds its Odoo again from
  the pid file.
- Odoo runs as the user of OmniaCLI: that user needs write access to Odoo's
  `data_dir` (filestore, sessions).
- Odoo is not started at boot: a Restart starts it.

## MCP interface

OmniaCLI serves an MCP endpoint at `/mcp` (streamable HTTP) for AI clients.
It needs the `mcp` SDK, so Python 3.10 or newer, which the package always
provides: OmniaCLI installed by hand on Python 3.9 runs without it.

| Tool | What it does |
|---|---|
| `list_servers` | every server, active or draft, with its instances |
| `server_status` | repositories (branch, last commit), Odoo running, CPU and memory, auto update, last jobs |
| `pending_updates` | commits not yet pulled, per repository (`git fetch`) |
| `read_log` | the last lines of an instance's log, optionally only ERROR/CRITICAL with their tracebacks |
| `list_jobs`, `get_job` | the jobs, filtered by server and text, and a job's log |
| `update_test` | **the only action on Odoo**: git pull + update of the changed modules + restart of the **test** instance |
| `backup_test` | a backup (database and filestore) of the **test** instance; `options: {"no_filestore": true}` for the database only |
| `backup` | (admin token) a backup of any instance, **production** too; the same `options` |
| `list_backups` | (admin token) an instance's backups, each with its download path |
| `read_server` | (admin token) a server's whole configuration, active or draft |
| `add_draft_server` | (admin token) a new server, always a **draft**: it goes live only with Test & activate on the web |
| `dev_setup` | what a client needs to rebuild an instance locally: remote URLs (without credentials), branches, commits, `addons_path` as repository folders, Odoo and Python versions, the Odoo config options without secrets nor server paths, and the backups to download |

With an admin token `dev_setup`, `backup` and `list_backups` also reach the
draft servers (saved complete), and their backups download with that token;
every other tool works on the active servers only.

Access is by bearer token, never by the users' passwords. Create one per
client on the **Users** page (the token is shown once), or by hand: add its
hash to `auth.tokens`:

```bash
omniacli new-token dev-laptop --role admin
```

```yaml
auth:
  users: ...
  tokens:
    dev-laptop: {token: "sha256$...", role: admin}
```

A token has a role, as a user does: `user` reads, `developer` also runs
`update_test` and `backup_test` and downloads backups, `admin` also has the
tools marked admin above. A client sees only the tools of its role. A token
saved without a role (a bare hash, as before roles) is a `developer`.

Then, in Claude Code:

```bash
claude mcp add --transport http omniacli https://omniacli.example.com/mcp \
    --header "Authorization: Bearer <token>"
```

A token also opens the pages for reading, which is how a client downloads a
backup (`GET <base URL>/backups/...` with the same header); it can never
POST to them.

## Run

```bash
omniacli serve -c omniacli.yaml --host 127.0.0.1 --port 8090
```

### Login and security

Users log in on the login page; the browser then holds a session cookie
(`HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS) for **8 hours** from the
login, and **Log out** ends it on the server. The server keeps only a hash of
each session, in `sessions.json` next to `jobs_dir` (mode 600), so a restart
does not log anybody out; a changed password or a deleted user ends that
user's sessions at once. After 5 wrong logins within 15 minutes from an
address or for a user name, every further try waits 30 seconds, doubled each
time up to 15 minutes. HTTP Basic is not accepted: scripts and AI clients use
tokens (MCP), other OmniaCLI instances use link tokens.

Every page is sent with `X-Frame-Options: DENY` and `Content-Security-Policy:
frame-ancestors 'none'` (no framing by other sites), `X-Content-Type-Options:
nosniff`, `Referrer-Policy: same-origin`, and over HTTPS
`Strict-Transport-Security`. Expose OmniaCLI only through an HTTPS reverse
proxy that keeps the `Host` header: POSTs are accepted only when their
`Origin` matches `Host`, which blocks cross-site requests, and the password
must never travel in clear. The same holds for the WebSocket of the shell,
which the proxy must pass on. Without it the shell page says "refused or
connection lost".

#### nginx

A complete site for nginx: HTTPS on port 8443, open to the LAN only, in
front of OmniaCLI listening on `127.0.0.1:8090` (`omniacli serve --host
127.0.0.1 --port 8090`, as the package runs it). Put the certificate and its
key in `/etc/nginx/omniacli/`, the file in `/etc/nginx/sites-available/`
(linked from `sites-enabled/`) or in `/etc/nginx/conf.d/`, write your own
networks in `allow`, then `nginx -t` and `systemctl reload nginx`.

```nginx
server {
    listen 8443 ssl;
    server_name _;

    ssl_certificate     /etc/nginx/omniacli/omniacli.crt;
    ssl_certificate_key /etc/nginx/omniacli/omniacli.key;

    # the LAN only
    allow 10.11.1.0/24;
    allow 172.20.1.0/24;
    deny  all;

    location / {
        proxy_pass http://127.0.0.1:8090;

        # the WebSockets of the shell, Claude and psql pages and of the tunnels
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # OmniaCLI accepts a POST (and a WebSocket) only when its Origin
        # matches Host: pass the Host the browser sent, port included.
        # Not $host, which drops the port: every POST would be refused
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # backup downloads and the pages of linked instances are streamed;
        # an idle shell stays open up to the read timeout
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Each `proxy_read_timeout` once: nginx refuses a directive set twice in the
same `location` ("directive is duplicate"). And `Host` once: two
`proxy_set_header Host` lines send two Host headers, and OmniaCLI answers
400.

The networks in `allow` are those of the clients, not the server's: a PC
outside them gets 403.

#### The certificate

On a LAN without a CA of its own, a self-signed certificate, made on the
nginx machine. Its `subjectAltName` lists every name and IP the users type
in the browser (browsers and Python check only that, not the CN): with no
DNS name, the IP alone; with one, `DNS:<name>,IP:<ip>`.

```bash
sudo mkdir -p /etc/nginx/omniacli
cd /etc/nginx/omniacli
sudo openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
  -keyout omniacli.key -out omniacli.crt \
  -subj "/CN=10.11.1.20" \
  -addext "subjectAltName=IP:10.11.1.20" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
  -addext "extendedKeyUsage=serverAuth"
sudo chmod 600 omniacli.key
sudo nginx -t && sudo systemctl reload nginx
```

No `-addext basicConstraints=…`: Debian's openssl config already sets
`CA:TRUE`, and a second one makes the certificate invalid ("unable to get
local issuer certificate"). The IP must not change (a static IP or a DHCP
reservation): a new IP or a new name means a new certificate, copied again
to every client. Then:

- browsers: import `omniacli.crt` among the trusted authorities, or accept
  the warning;
- another OmniaCLI linking to this one: `omniacli.crt` as the link's **CA
  file** (see "A link or an MCP client to an OmniaCLI behind a self-signed
  certificate" above);
- Claude Code: `NODE_EXTRA_CA_CERTS=<path>/omniacli.crt`.

With a customer's own CA, its certificate for the server's name or IP takes
the place of `omniacli.crt`, and the clients get the CA's certificate.

Tried with OpenSSL 1.1.1w: `openssl verify -x509_strict -purpose sslserver
-verify_ip <ip>` passes, Python 3.9 accepts it as `cafile`, nginx 1.22 loads
it and `curl --cacert` goes through.

## Develop

```bash
pip install -e '.[test]'
pytest
```

The tests use real git repositories in a temporary folder, and fake
`click-odoo-*` scripts and restart commands.

### Plugins

A type of server (Odoo, and later others) is a plugin: a subclass of
`omniacli.plugin_api.ServicePlugin`, declared by its package as an entry point
of the group `omniacli.plugins`:

```toml
[project.entry-points."omniacli.plugins"]
odoo = "omniacli_odoo:OdooPlugin"
```

`omniacli/plugin_api.py` is the contract and the only module a plugin
imports. The core does what every type shares (the git repositories, the
service, the jobs, the auto update, users and links); the plugin reads its own
settings of an instance, checks it for Test & activate, does what follows a
pull, the backup and the command of a process OmniaCLI starts. A server names
its type in the servers file (`type: odoo`; none is `odoo`). At start every
plugin is checked against the contract (plugin API version, the methods it
must have, its name, its own `self_test`): one that fails is disabled, and
the **Plugins** page (admins) says why. The servers of a type without its
plugin are kept as written, without actions, until it is back.

A plugin may also give:

- **`fields()`**: its settings of an instance, which the server form draws
  (with buttons next to a field that call the plugin's routes: Odoo's
  *Reload* of the Odoo config, its *pip* buttons);
- **`actions()`**: buttons of its own on the server page. The core checks the
  role, runs the action as a job of the instance (or of the whole server) and
  draws a confirm, a dialog of parameters, or a dialog of the plugin's own
  template; they are posted to `/servers/<server>/<role>/x/<plugin>/<action>`;
- **`router(host)`**: a FastAPI router served under `/x/<plugin>/` (login
  needed; `host.needs(role)` for more), with its own templates
  (`templates_dir`, which may extend `base.html`) and static files
  (`static_dir`, at `/x/<plugin>/static/`);
- **`dev_setup()`** and **`mcp_help`**: its part of the MCP tool `dev_setup`
  and of the MCP instructions;
- **`backup_fields()`**: the choices of a backup (Odoo: without filestore),
  given to **`backup()`** as options; a checkbox not posted is off, and off
  is the backup made without any choice;
- **`restore()`** and **`restore_fields()`**: load a backup zip into an
  instance, with the choices asked first (Odoo: neutralize). The core's
  *Copy from link* is offered only for a type that has it.
- **`script_runner()`** and **`script_env()`**: how a scheduled action runs a
  Python script on an instance (the python, the system user, the
  environment) and the `OMNIACLI_*` variables it adds (Odoo: the venv's
  python as `run_as`, `OMNIACLI_DATABASE`, `OMNIACLI_ODOO_CONFIG`). Without
  them: OmniaCLI's own python, as OmniaCLI's user.
- **`db_shell()`**: the database client of the psql page (the command and
  its environment); without it, no psql button.
- **`tunnel_address()`**: where a tunnel's connection goes, from the path
  of its first request (Odoo: the bus to the gevent port); by default the
  web port on `127.0.0.1`.
- **`discover()`**: the instances of the type found on this machine, for
  the Discover page (`Discovered`: what was read, the instance's mapping,
  the notes); without it, the type finds nothing.

Two plugins are built in: **generic** (git repositories and a systemd or
init.d service, nothing else: what an install without plugins has) and
**Odoo** (`omniacli/odoo`, which imports only `plugin_api`). The core imports
neither: it reaches them through the contract only.

## Acknowledgements

OmniaCLI's work on the Odoo instances rests on
[click-odoo](https://github.com/acsone/click-odoo) and
[click-odoo-contrib](https://github.com/acsone/click-odoo-contrib) by
[ACSONE SA/NV](https://github.com/acsone) and their contributors:
`click-odoo-update` updates the changed modules and `click-odoo-backupdb`
backs up the databases. Without their work this interface would not have
been possible: thank you.

OmniaCLI also builds on [Odoo](https://www.odoo.com), and on FastAPI,
Starlette, Uvicorn, websockets, Jinja, HTTPX, PyYAML, croniter, cryptography and
the MCP Python SDK.
The terminal of the shell page is [xterm.js](https://github.com/xtermjs/xterm.js)
with its fit addon (MIT, shipped in `omniacli/static`).

## License

Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C.

OmniaCLI is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License
in [LICENSE](LICENSE) for more details.

Whoever runs a modified OmniaCLI for users over a network must offer those
users the source code of that version (section 13 of the license).
