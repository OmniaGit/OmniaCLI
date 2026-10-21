# OmniaCI

A small web service, protected by HTTP Basic authentication, that deploys
Odoo instances. Each **server** has a **production** and/or a **test**
instance. An instance has one or more git working copies, a database, an Odoo
config file, a log file and a systemd or init.d service.

## What it does

| Action | Production | Test | What runs |
|---|---|---|---|
| Git pull | ✓ | ✓ | `git pull --ff-only <remote> <branch>` in every repository |
| Git pull + restart | ✓ | ✓ | pull, `click-odoo-update` (only the changed modules), restart |
| Update modules | ✓ | ✓ | a popup asks: the changed modules (`click-odoo-update`) or chosen ones (`odoo-bin -u … --stop-after-init --workers=0`), and whether to restart after |
| Restart | ✓ | ✓ | the instance's systemd or init.d restart, or OmniaCI's own start of `odoo-bin` (see below) |
| Backup | ✓ | ✓ | `click-odoo-backupdb` → `<backup_dir>/<server>/<role>/<db>_<timestamp>.zip` |
| Merge test to production | | ✓ | backup production, merge, push, update modules, restart production |
| Log | ✓ | ✓ | `tail -F` of the Odoo log in the browser, with an "errors only" filter |

- **Servers page**: one row per live server, with the CPU of production and
  test over the last 15 minutes and the buttons **Test**, **Production** and
  **Both**, which open the server's page on that instance or on both side by
  side: CPU and memory charts of the last hour, the actions, the settings and
  the live log. *Merge test to production* is on that page.
- **Load**: every 5 seconds OmniaCI samples each live instance. A systemd
  instance is measured on its unit's cgroup (cgroup v2): CPU of the main
  process and every worker, memory as the resident size of its processes. An
  init.d instance is measured on the processes started with `-c <its Odoo
  config>`. CPU is in percent of one core.
- **Draft and Test & activate**: a new or cloned server is a draft (no page,
  no actions, no auto update). *Test & activate* runs real checks as the
  OmniaCI user and makes it live only when no check fails: Odoo config,
  addons path, log file, click-odoo scripts and an `import click_odoo, odoo`
  with the instance's python, the database (connected with the Odoo config's
  credentials), odoo-bin, every repository (branch, no local change, remote
  reachable), the service and `sudo -n -l` for its restart command, separate
  folders and databases for production and test, the backup folder. Saving
  a live server runs the same checks: if they fail nothing is saved and it
  stays live. `omniaci check-config` runs them from the command line.
- **Auto update**: per instance, every `interval_minutes` within `window`,
  OmniaCI fetches every repository. When there are new commits it runs
  *git pull + restart* as an `auto` job.
- **Jobs**: every action is a job. Its output, its status and its error are
  shown live in the web page, and the last 500 are kept in `jobs_dir`.
- Only one job runs on an instance at a time; the others wait.

### Rules the actions follow

- A pull is **fast-forward only**. A repository on a branch other than the
  configured one, or with uncommitted changes, stops the job: OmniaCI never
  creates merge commits or throws away local work on a pull.
- **Merge test to production** merges the commit **checked out on the test
  server**, not the tip of the test branch, so production gets exactly what
  was tested. That commit must already be on the remote. Test and production
  repositories are paired by `name`. If any repository conflicts, the merge
  is aborted, every production repository is reset to the commit it had
  before, and nothing is pushed: the conflict is for a person to resolve.
- If a module update fails, the instance is **not** restarted.

## Install from the Debian package

Supported: Debian 11, 12, 13 and Ubuntu 22.04, 24.04 (amd64), each with its
system `python3`.

```bash
sudo apt install ./omniaci_<version>_all.deb
sudo cat /etc/omniaci/initial-admin-password     # password of user admin
```

The package:

- creates the system user `omniaci` and runs the service `omniaci` as that
  user, on `127.0.0.1:8090` (change it in `/etc/default/omniaci`);
- builds `/opt/omniaci/venv` at installation with the system `python3` and the
  dependency versions locked for that Python (`packaging/debian/requirements`).
  **The machine must reach PyPI while installing.** After an upgrade of the
  system Python (a distribution upgrade), rebuild it with
  `sudo dpkg-reconfigure omniaci`;
- creates `/etc/omniaci/omniaci.yaml` once, with a random password for
  `admin`; upgrades never change it;
- keeps the servers file, the jobs and the backups in `/var/lib/omniaci`;
- installs `/etc/sudoers.d/omniaci`, all comments, to be completed;
- on purge, deletes configuration and jobs but keeps the backups.

### Build and test the package

```bash
packaging/debian/build_deb.sh              # test build -> share, omniaci_<year>.1.<n>_all.deb
packaging/debian/build_deb.sh --release    # release build of branch <year> -> share, OmniaCI.deb
packaging/debian/build_deb.sh --check DIR  # the working tree, into DIR, no tag
packaging/debian/test_deb.sh <deb>         # install and check it in Docker, systemd included
packaging/debian/make_locks.sh             # regenerate the dependency locks, with the tests
```

## Install by hand

```bash
python3.11 -m venv /opt/omniaci/venv
/opt/omniaci/venv/bin/pip install /path/to/OmniaCI
```

The `click-odoo-*` scripts import the instance's own `odoo` package. So
`click-odoo-contrib` must be installed **in the venv that runs each Odoo**,
not in OmniaCI's venv. `venv_bin` in the config points to it.

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

The configuration has two files:

- the **main config** (`omniaci.yaml`): users, `backup_dir`, `jobs_dir` and
  `servers_file`. It is edited by hand and the web interface never writes it.
- the **servers file** (`servers_file`, see `servers.example.yaml`): the
  servers and their instances. It is edited from the web interface
  (**Configuration** page: new, edit, clone, delete), so the user running
  OmniaCI must be able to write it. Every save is validated like the file at
  startup, the previous version is kept as `<file>.bak`, and the auto update
  loops restart with the new settings. Clone copies a server's configuration
  only (no database, no files). A server with a running job cannot be edited or
  deleted.

```bash
cp config.example.yaml omniaci.yaml
omniaci hash-password            # paste the result under auth.users
omniaci check-config -c omniaci.yaml
```

`check-config` checks that every path is readable, that the click-odoo
scripts exist and that every repository is on its configured branch.

### Permissions

The user that runs OmniaCI needs:

- to **own** the repositories (or have them listed in its git
  `safe.directory`): git refuses to work in a repository owned by another
  user ("dubious ownership"). It also needs git credentials for `fetch`/`push`
  that need no prompt (an SSH key without a passphrase, or a credential
  helper). Git is never allowed to ask.
- read access to the Odoo config and log files.
- a database connection: `click-odoo-*` connect with the settings of the
  instance's Odoo config file.
- the right to restart the services without a password. With `sudo: true`
  (the default), the command runs as `sudo -n`. sudoers needs the full path
  that `command -v systemctl` prints on that machine. Example
  `/etc/sudoers.d/omniaci`:

```
omniaci ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password restart odoo-customer-a
omniaci ALL=(root) NOPASSWD: /etc/init.d/odoo-customer-a-test restart
```

## Odoo started by OmniaCI (restart type `process`)

For an Odoo that has no service, OmniaCI can start it itself:
`<venv_bin>/python <odoo-bin> -c <odoo_config>`, in its own session. Its
output goes to `<jobs_dir>/../logs/<server>_<role>.log` (the logfile of the
Odoo config is overridden), and that file is the instance's log; its pid is
kept next to it (`.pid`).

- Restart stops OmniaCI's Odoo (SIGTERM to its process group, SIGKILL after
  60 s) and starts it again. It refuses when another process runs with the
  same Odoo config (started from a console, an IDE, a service): OmniaCI
  never stops a process it did not start.
- Stop stops it, and it stays stopped until Start (the same button as
  Restart): the auto update still pulls and updates its modules, but does
  not start it again. Stop exists only for this type: a service is stopped
  with its own tools.
- Odoo keeps running when OmniaCI stops, restarts or is upgraded: the
  package's unit has `KillMode=process`, and OmniaCI finds its Odoo again from
  the pid file.
- Odoo runs as the user of OmniaCI: that user needs write access to Odoo's
  `data_dir` (filestore, sessions).
- Odoo is not started at boot: a Restart starts it.

## Run

```bash
omniaci serve -c omniaci.yaml --host 127.0.0.1 --port 8090
```

Basic authentication sends the password with every request. So expose
OmniaCI only through an HTTPS reverse proxy that keeps the `Host` header:
POSTs are accepted only when their `Origin` matches `Host`, which blocks
cross-site requests.

## Develop

```bash
pip install -e '.[test]'
pytest
```

The tests use real git repositories in a temporary folder, and fake
`click-odoo-*` scripts and restart commands.
