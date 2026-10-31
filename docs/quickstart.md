# Quickstart

Get OmniaCLI running on a Debian or Ubuntu machine, log in, and put your
first Odoo instance under its control. It takes about 15 minutes on a machine
where Odoo already runs. Every step links to the section of the
[README](../README.md) that explains it in full.

## What you need

| | |
|---|---|
| **Operating system** | Debian 11, 12, 13 or Ubuntu 22.04, 24.04, amd64 |
| **Python** | 3.10 or newer. On Debian 11 (Python 3.9) the package uses a local `python3.10`+ if there is one, otherwise it downloads Python 3.12 into `/opt/omniacli/python` |
| **Network while installing** | PyPI (the package builds its venv there), and `github.com` when Python 3.12 has to be downloaded |
| **Access** | `sudo` on the machine |
| **A reverse proxy** | nginx, or another one, with HTTPS: OmniaCLI listens on `127.0.0.1:8090` only |
| **Your Odoo instances** | on the same machine: their configs, logs, repositories and systemd services |

The package pulls in `python3-venv`, `git`, `ca-certificates`, `adduser`
and `init-system-helpers` by itself.

## Step 1: Get the package

Every [release](https://github.com/OmniaGit/OmniaCLI/releases) carries the
package twice: `omniacli_<version>_all.deb`, and `OmniaCLI.deb`, whose link
always gives the latest one.

```bash
wget https://github.com/OmniaGit/OmniaCLI/releases/latest/download/OmniaCLI.deb
```

## Step 2: Install it

```bash
sudo apt install ./OmniaCLI.deb
```

The installation:

- creates the system user `omniacli` and starts the service `omniacli` as
  that user, on `127.0.0.1:8090`;
- builds `/opt/omniacli/venv` with the dependency versions locked for your
  Python;
- writes `/etc/omniacli/omniacli.yaml` once, with a random password for the
  user `admin`, and saves that password in
  `/etc/omniacli/initial-admin-password` (readable by root only);
- keeps the servers, jobs and backups in `/var/lib/omniacli`;
- installs `/etc/sudoers.d/omniacli`, all comments, for you to complete in
  step 5.

Read the password of `admin`:

```bash
sudo cat /etc/omniacli/initial-admin-password
```

An upgrade later is the same `apt install` of a newer package: the
configuration, the servers, the users, the jobs and the backups stay.

> On a development workstation, where the repositories and the Odoo configs
> are your own, run the service as your user instead: see
> [The service as another user](../README.md#the-service-as-another-user-omniacli_user).

## Step 3: Check the service

```bash
systemctl status omniacli
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/login
```

The service is `active (running)` and the login page answers `200`. If not,
`journalctl -u omniacli` tells why.

To change the address or the port, edit `/etc/default/omniacli`
(`OMNIACLI_HOST`, `OMNIACLI_PORT`), then `sudo systemctl restart omniacli`.

## Step 4: Put HTTPS in front of it and log in

OmniaCLI must be reached only through an HTTPS reverse proxy that passes the
`Host` header and the WebSockets on: the password must never travel in clear,
and a POST is accepted only when its `Origin` matches `Host`.

The README has a complete nginx site and the commands for a self-signed
certificate on a LAN:

- [nginx](../README.md#nginx)
- [The certificate](../README.md#the-certificate)

Then open `https://<your server>:8443/` (the port of that example) and log
in as `admin` with the password of step 2.

`admin` is set in the main configuration file, so its password is changed
there, not on the **Users** page:

```bash
/opt/omniacli/venv/bin/omniacli hash-password     # asks the password twice, prints its hash
sudoedit /etc/omniacli/omniacli.yaml              # paste the hash as auth.users.admin
sudo systemctl restart omniacli
```

The other users are created on the **Users** page.

## Step 5: Give OmniaCLI access to your Odoo

The usual machine: Odoo runs as the system user `odoo`, its configs and logs
readable by group `odoo` only, one systemd service per instance. As root:

1. **Read the configs and logs** (only if the group `odoo` exists):

   ```bash
   getent group odoo && sudo adduser omniacli odoo
   sudo systemctl restart omniacli
   sudo -u omniacli head -c 1 /etc/odoo/odoo.conf; echo " $?"   # 0: readable
   ```

2. **Run the Odoo venv's programs as `odoo`**, in
   `/etc/sudoers.d/omniacli`:

   ```
   omniacli ALL=(odoo) NOPASSWD:SETENV: /opt/odoo/venvs/odoo18/bin/
   ```

3. **Restart, start and stop each Odoo service**, one exact line per service
   and action, never a wildcard:

   ```
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password restart odoo-prod
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password start odoo-prod
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password stop odoo-prod
   ```

   After every change: `sudo visudo -cf /etc/sudoers.d/omniacli` (a broken
   sudoers file can stop every `sudo` of the machine).

4. **The repositories**: git runs as `omniacli`, which must own them and
   reach their remotes without a prompt (a deploy key, or the git
   credentials of the interface).

You do not need to guess which lines are missing: the checks of step 6 print
them. Paths and service names above are examples. Details:
[First setup on a machine where Odoo already runs](../README.md#first-setup-on-a-machine-where-odoo-already-runs).

## Step 6: Add your first server

1. Open **Servers → Discover**: it finds every Odoo running on the machine,
   and every systemd unit that starts one. Save the ones you want as drafts.
2. Open each draft and complete what its notes say.
3. Run **Test & activate**. Each failed check says what is missing, with the
   sudoers line or the command to add. Fix, run it again, until the server
   is active.

The server now appears on the **Dashboard** with the state of its Odoo and the
Start / Stop / Restart buttons.

## Step 7: Run your first job

On the server's page, press **Git pull + restart** on the test instance:
OmniaCLI pulls the repositories, updates the changed modules
(`click-odoo-update`) and restarts it. The job runs live on the **Jobs**
page, and the Odoo log is shown on the server's page.

Before the first update of a database, read
[Install by hand](../README.md#install-by-hand): `click-odoo-update` must be
installed in the venv of each Odoo, and run once with
`--only-compute-hashes`, or it updates every module the first time.

## Optional: finish the setup later

| What | Where |
|---|---|
| More users, with roles (user, developer, admin) | **Users** |
| Tokens for AI clients (Claude Code and others) | **Users**, see [MCP interface](../README.md#mcp-interface) |
| Other OmniaCLI machines operated from this one | **Links**, see [Linked instances](../README.md#linked-instances) |
| Pulls, database copies and scripts at fixed times | **Schedules**, see [Scheduled actions](../README.md#scheduled-actions) |
| Headings of the Dashboard | **Edit groups** |
| Git user and token per host | see [Git credentials](../README.md#git-credentials) |

## Before you open it to more people

- Change the password of `admin` (step 4) and give each person their own
  user, with the lowest role that does the job.
- Keep OmniaCLI on `127.0.0.1` and reach it only through the HTTPS proxy; in
  nginx, `allow` only the networks of your clients.
- Never write a wildcard in the restart lines of sudoers.
- Make a backup of each production instance and download it once, so you know
  the restore works before you need it.

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| `apt install` stops with "the venv could not be built (is PyPI reachable?)" | The machine cannot reach PyPI. Open it, then `sudo dpkg --configure -a` |
| "Python could not be downloaded … (is github.com reachable?)" | Debian 11 without a local Python 3.10+: open `github.com`, or install a `python3.10`+ first, then `sudo dpkg --configure -a` |
| OmniaCLI stopped working after a distribution upgrade | The venv was built on the old Python: `sudo dpkg-reconfigure omniacli` |
| "Odoo config: Permission denied", "Log file … is not readable" | `omniacli` cannot read them: step 5.1, or ACLs ([Configuration cases](../README.md#configuration-cases)) |
| "fatal: detected dubious ownership in repository" | The repositories belong to another user: give them to `omniacli` ([Configuration cases](../README.md#configuration-cases)) |
| "could not read Username for 'https://github.com': terminal prompts disabled" | `omniacli` has no git credentials: a deploy key and an SSH remote ([Configuration cases](../README.md#configuration-cases)) |
| "Failed to restart … Interactive authentication required" | **Use sudo -n** off in the server form, or the sudoers line missing: step 5.3 |
| Every POST refused behind nginx, or the shell page says "refused or connection lost" | The proxy does not pass `Host` with its port, or the WebSockets: use `proxy_set_header Host $http_host` and the `Upgrade` headers of the [nginx](../README.md#nginx) example |
| Answer 400 behind nginx | Two `proxy_set_header Host` lines: keep one |

## What's next

- [What it does](../README.md#what-it-does): every page and action.
- [Rules the actions follow](../README.md#rules-the-actions-follow): what a
  deploy, a merge and a copy do, and in which order.
- [Configure](../README.md#configure): the configuration files.
- [Odoo in Docker Compose](../README.md#odoo-in-docker-compose-type-docker)
  and [Workspaces](workspaces.md).
- [Plugins](../README.md#plugins): other types of server.
