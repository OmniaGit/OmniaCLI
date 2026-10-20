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
| Backup | ✓ | ✓ | `click-odoo-backupdb` → `<backup_dir>/<server>/<role>/<db>_<timestamp>.zip` |
| Merge test to production | | ✓ | backup production, merge, push, update modules, restart production |
| Log | ✓ | ✓ | `tail -F` of the Odoo log in the browser, with an "errors only" filter |

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

## Install

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

```bash
cp config.example.yaml omniaci.yaml
omniaci hash-password            # paste the result under auth.users
omniaci check-config -c omniaci.yaml
```

`check-config` checks that every path is readable, that the click-odoo
scripts exist and that every repository is on its configured branch.

### Permissions

The user that runs OmniaCI needs:

- write access to the repositories, and git credentials for `fetch`/`push`
  that need no prompt (an SSH key without a passphrase, or a credential
  helper). Git is never allowed to ask.
- read access to the Odoo config and log files.
- a database connection: `click-odoo-*` connect with the settings of the
  instance's Odoo config file.
- the right to restart the services without a password. With `sudo: true`
  (the default), the command runs as `sudo -n`. Example `/etc/sudoers.d/omniaci`:

```
omniaci ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password restart odoo-customer-a
omniaci ALL=(root) NOPASSWD: /etc/init.d/odoo-customer-a-test restart
```

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
