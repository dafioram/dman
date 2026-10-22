# dman

A terminal manager for the Docker host it runs on, built around Docker Compose
projects. It is a menu-driven alternative to web UIs like Arcane or Portainer that
works over SSH, needs no daemon or database, and uses only Python's standard library.

## Requirements

- Linux (Debian is the main target)
- Python 3.9 or newer (`python3`)
- Docker Engine with the Docker CLI and the Compose plugin (`docker compose`)
- Access to the local Docker socket: run as root (`sudo dman`) or as a member of the `docker` group

## Install

From the unpacked folder:

```sh
sudo ./install.sh
```

The installer asks where your Compose projects live (default `/opt/docker`) and writes
`/etc/dman/dman.conf`. Use `sudo ./install.sh --yes` to skip questions.

It installs:

| Path | What |
|---|---|
| `/usr/local/bin/dman` | the command (a small launcher) |
| `/usr/local/lib/dman/` | `dman.py`, `VERSION`, `.dman-install-marker` |
| `/etc/dman/dman.conf` | config, created only if it does not exist |

The installed copy does not depend on the unpacked folder. To update, unpack the new
version and run `sudo ./install.sh` again: it replaces the old install in place (no
backups) and keeps your config. The installer refuses to overwrite anything at those
paths that a dman install did not create, and never touches Docker or your containers.

## Uninstall

```sh
sudo ./uninstall.sh            # asks first; asks separately about the config file
sudo ./uninstall.sh --purge    # also removes /etc/dman/dman.conf
```

Only the files listed above are removed. Docker, containers, images, volumes, Compose
files and the unpacked folder are left alone.

## Use

```sh
dman              # interactive menu
dman --version    # e.g. "dman 1"
dman --help
dman --config /path/to/other.conf
```

Main menu:

1. **Compose projects**: every project found in your project folders, plus any project
   Docker knows about from container labels. Status, Up, Up with build, Pull & up,
   Restart, Stop, Start, Logs, Down, Down + remove volumes, the containers of the
   project, the compose file(s), the resolved config and the environment. Projects
   with several services also get **One service**: logs, restart, stop, start, up,
   up with build or pull & up for just that service.
2. **Containers**: all containers with state, health, restart count, published ports,
   project and age. A running container that its restart policy keeps restarting is
   shown in yellow. Logs (last 100 / 500 / all / custom, since a time such as `15m` or
   `2026-10-22`, or follow live; `t` toggles timestamps), start/stop/restart, a shell
   (bash, falling back to sh), one-off commands, details, environment, mounts, removal.
3. **Live resource usage**: CPU, memory, network and disk I/O now; `a` auto-refreshes.
4. **Images**: size, age, which containers use each one; pull and remove.
5. **Volumes & mounts**: Docker volumes and bind mounts, which containers use them,
   their project; `z` fetches sizes on demand; remove unused volumes.
6. **Disk usage & cleanup**: `docker system df`, plus reviewed cleanup of stopped
   containers, unused images and build cache. Volumes are never removed here.
7. **System info**: versions, the Docker endpoint, the config file and project folders.

Keys in lists: a number opens an item, `n`/`p` page, `s` sort, `f` filter, Enter
refreshes, `h` help, `b` back, `q` quit. Ctrl-C at a prompt goes back; Ctrl-C while a
command runs (logs, up, a shell) stops that command and returns to dman. Long output
opens in `less` (`q` closes it).

## Configuration

dman reads the first file that exists of: `--config FILE`, `$DMAN_CONFIG`,
`~/.config/dman/dman.conf`, `/etc/dman/dman.conf`.

```ini
[projects]
roots =
    /opt/docker
    /srv/stacks
depth = 2

[ui]
page_size = auto
```

`depth` is how far below each folder dman looks: with 2, both `/opt/docker/app/compose.yaml`
and `/opt/docker/group/app/compose.yaml` are found. Hidden folders and `node_modules` are skipped.

## How projects are found

- A folder with `compose.yaml`, `compose.yml`, `docker-compose.yaml` or
  `docker-compose.yml` is a project. Its matching `*.override.yaml|yml` is included,
  just as `docker compose` does on its own.
- Variant files such as `docker-compose.prod.yml` are shown too. If a folder has only
  one compose file, that file is used. If it has several and no standard one, the
  project shows `pick files`; open it and use **Choose compose files** (the choice
  lasts until dman exits; once deployed, the files used are remembered by Docker).
- A project that is running or has stopped containers is matched to its folder through
  the labels Compose puts on containers, and its actions use exactly the files it was
  deployed with.
- Projects started from a folder outside your configured folders still appear while
  their containers exist. If their compose files are gone, they show `(no file)`:
  status, logs, start/stop/restart and Down still work; Up and anything that needs the
  file are disabled.
- A project in your folders that has never been started shows `not deployed` and can
  be started with **Up**. Its name is guessed from the folder; Compose has the final
  say (for example a `name:` in the file).

## Safety

- **Local only.** dman works out which daemon the Docker CLI would use (context,
  `DOCKER_HOST`, `DOCKER_CONTEXT`) and refuses to start unless it is a Unix socket on
  this machine. It then pins every Docker and Compose call to that socket.
- **No host shell.** Every command runs as an argument list. The "Run a command"
  action splits your line like a shell would (quotes work) but does not interpret
  pipes, redirects, `$VARS` or `*`; use `sh -c '...'` inside the container for that.
- **Review, confirm, re-check.** Removals show exactly what they affect and ask first.
  Right before running, dman checks the targets again; if anything changed (a container
  started, a volume became used, a project gained a container), it cancels and asks you
  to review again.
- **Down + remove volumes** (`docker compose down -v`) lists the volumes it will
  delete, says when it cannot be sure, and needs the project name typed. It is never
  run as part of anything else, and plain **Down** never turns into it.
- **Volumes** are only deleted by explicit volume actions: the volume screen (unused
  volumes only, typed name) or Down + remove volumes.
- **Secrets.** Environment views hide values until you ask to reveal them. Raw inspect
  and the resolved Compose config ask first, since they contain values too.
- **Terminal output.** Text that comes from Docker or from files (names, labels, health
  check output, compose files, error messages) has control characters removed before
  it is shown, so it cannot move the cursor, change the window title or write to the
  clipboard. Log and command output (Logs, a shell, Up) is shown as Docker prints it.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests cover parsing, project discovery, endpoint checks and the down -v preview,
with no Docker daemon needed.

Integration tests run against the local Docker daemon. They create and then remove a
throwaway Compose project (built `FROM scratch`, containers never started), so they
only run when asked:

```sh
DMAN_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

GitHub Actions (`.github/workflows/ci.yml`) runs the unit tests on Python 3.9-3.13,
the integration tests on a runner with Docker, ShellCheck on the scripts, and an
install / reinstall / uninstall cycle into a temporary prefix.

## Version

`VERSION` holds one whole number. Each published package revision gets the next
number; `dman --version` reads it from the installed copy.
