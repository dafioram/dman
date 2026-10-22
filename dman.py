#!/usr/bin/env python3
"""dman - a terminal manager for a local Docker host run mostly with Docker Compose.

Standard library only. Requires Python 3.9+, the Docker CLI and the Compose plugin.
Every Docker call is made with an argument list (never through a host shell).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 9):
    sys.stderr.write("dman needs Python 3.9 or newer (found %d.%d).\n" % sys.version_info[:2])
    sys.exit(1)

import argparse
import configparser
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

APP = "dman"
HERE = os.path.dirname(os.path.realpath(__file__))
USER_CONFIG = os.path.expanduser("~/.config/dman/dman.conf")
SYSTEM_CONFIG = "/etc/dman/dman.conf"
DEFAULT_DEPTH = 2
MAX_DEPTH = 5

COMPOSE_STANDARD = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"]
COMPOSE_FILE_RE = re.compile(r"^(?P<base>docker-compose|compose)(?:\.(?P<mid>[A-Za-z0-9_.-]+?))?\.ya?ml$")
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv"}

L_PROJECT = "com.docker.compose.project"
L_SERVICE = "com.docker.compose.service"
L_WORKDIR = "com.docker.compose.project.working_dir"
L_FILES = "com.docker.compose.project.config_files"
L_ANON = "com.docker.volume.anonymous"
L_CVOLUME = "com.docker.compose.volume"


# --------------------------------------------------------------------------- #
# Version
# --------------------------------------------------------------------------- #

def parse_version(text: str) -> Optional[int]:
    """VERSION holds exactly one positive integer, optionally followed by a newline."""
    if re.fullmatch(r"[1-9][0-9]*\n?", text or ""):
        return int(text.strip())
    return None


def read_version(path: Optional[str] = None) -> Optional[int]:
    try:
        with open(path or os.path.join(HERE, "VERSION"), encoding="utf-8") as fh:
            return parse_version(fh.read())
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #

class Style:
    enabled = False
    ellipsis = "~"
    codes = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
             "blue": "34", "magenta": "35", "cyan": "36"}


def init_terminal() -> None:
    Style.enabled = (sys.stdout.isatty() and "NO_COLOR" not in os.environ
                     and os.environ.get("TERM", "") != "dumb")
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    Style.ellipsis = "\u2026" if "utf" in enc else "~"


def c(text: str, *styles: str) -> str:
    styles = tuple(s for s in styles if s)
    if not Style.enabled or not styles:
        return text
    return "\033[" + ";".join(Style.codes[s] for s in styles) + "m" + text + "\033[0m"


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + Style.ellipsis


def term_size() -> Tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    return max(size.columns, 20), max(size.lines, 10)


# dman's own styles (see Style.codes) pass through; every other control character is
# dropped, so names, labels, file contents and Docker output cannot move the cursor,
# set the window title, write the clipboard (OSC 52) and so on.
_OWN_SGR = r"\x1b\[(?:[0-2]|3[1-6])(?:;(?:[0-2]|3[1-6]))*m"
_CONTROL_RE = re.compile(r"(%s)|[\x00-\x08\x0b-\x1f\x7f-\x9f]" % _OWN_SGR)


def strip_controls(text: str) -> str:
    return _CONTROL_RE.sub(lambda m: m.group(1) or "", text)


class SafeOutput:
    """Wraps sys.stdout and passes everything written through strip_controls.
    Other attributes (fileno, isatty, encoding, ...) are the real stream's, so input()
    still uses readline."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def write(self, text: str) -> int:
        self.stream.write(strip_controls(text))
        return len(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def clear() -> None:
    if sys.stdout.isatty():
        out = getattr(sys.stdout, "stream", sys.stdout)
        out.write("\033[H\033[2J")
        out.flush()
    else:
        print()


class QuitApp(Exception):
    """Raised anywhere to leave dman cleanly."""


def ask(prompt: str, on_interrupt: str = "b") -> str:
    """Read a line. Ctrl-C returns `on_interrupt`; Ctrl-D quits dman."""
    try:
        return input(prompt).strip()
    except KeyboardInterrupt:
        print()
        return on_interrupt
    except EOFError:
        print()
        raise QuitApp()


def pause(msg: str = "Press Enter to continue...") -> None:
    ask(c(msg, "dim"), on_interrupt="")


def confirm(question: str, default: bool = False) -> bool:
    answer = ask(question + (" [Y/n] " if default else " [y/N] "), on_interrupt="n").lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def typed_confirm(expected: str, what: str) -> bool:
    answer = ask("Type %s to %s (anything else cancels): " % (c(expected, "bold"), what),
                 on_interrupt="")
    return answer == expected


def title_line(text: str) -> None:
    print(c(text, "bold", "cyan"))


def human_bytes(n: Optional[float]) -> str:
    if n is None:
        return "-"
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(value) < 1000 or unit == "TB":
            return ("%d%s" % (value, unit)) if unit == "B" else ("%.1f%s" % (value, unit))
        value /= 1000.0
    return "-"


_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?")


def parse_ts(text: Optional[str]) -> Optional[datetime]:
    """Parse Docker timestamps (RFC 3339 with up to 9 fractional digits) on Python 3.9."""
    if not text or text.startswith("0001-"):
        return None
    m = _TS_RE.match(text.strip())
    if not m:
        return None
    date, clock, frac, tz = m.groups()
    frac = (frac or "")[:7]
    if frac:
        frac = frac.ljust(7, "0")    # Python < 3.11 accepts only 3 or 6 fractional digits
    tz = tz or "Z"
    if tz == "Z":
        tz = "+00:00"
    elif ":" not in tz:
        tz = tz[:3] + ":" + tz[3:]
    try:
        return datetime.fromisoformat("%sT%s%s%s" % (date, clock, frac, tz))
    except ValueError:
        return None


def ago(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    secs = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    for unit, size in (("y", 31536000), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%d%s" % (secs // size, unit)
    return "%ds" % secs


def local_time(dt: Optional[datetime]) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if dt else "-"


def parse_selection(text: str, count: int) -> Optional[List[int]]:
    """'1 3-5,7' or 'all' -> sorted 0-based indexes. None if anything is invalid."""
    t = text.strip().lower()
    if not t:
        return None
    if t in ("all", "*"):
        return list(range(count))
    picked = set()
    for part in re.split(r"[,\s]+", t):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            if a < 1 or b > count:
                return None
            picked.update(range(a - 1, b))
        elif part.isdecimal():
            k = int(part)
            if not 1 <= k <= count:
                return None
            picked.add(k - 1)
        else:
            return None
    return sorted(picked) or None


# --------------------------------------------------------------------------- #
# Running commands
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def message(self) -> str:
        return explain_error((self.err or self.out).strip() or "exit code %d" % self.rc)


def explain_error(msg: str) -> str:
    low = msg.lower()
    if "permission denied" in low and "docker" in low and "sock" in low:
        return (msg + "\nHint: your user cannot reach the Docker socket. Run dman with sudo, "
                "or add the user to the 'docker' group and log in again.")
    if "cannot connect to the docker daemon" in low or "failed to connect to the docker api" in low:
        return msg + "\nHint: is the Docker service running?  (sudo systemctl status docker)"
    return msg


def run(args: Sequence[str], timeout: float = 60, cwd: Optional[str] = None) -> Result:
    """Run a command with captured output. Never uses a shell."""
    args = list(args)
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace",
                           timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
        return Result(p.returncode, p.stdout, p.stderr)
    except FileNotFoundError:
        return Result(127, "", "command not found: %s" % args[0])
    except subprocess.TimeoutExpired:
        return Result(124, "", "timed out after %ds: %s" % (timeout, shlex.join(args)))
    except OSError as e:
        return Result(126, "", str(e))


class ChildSignals:
    """While a foreground child runs, Ctrl-C stops the child but not dman.

    A Python-level no-op handler (not SIG_IGN) is used so the child gets the default
    SIGINT behaviour after exec."""

    def __enter__(self) -> "ChildSignals":
        self.old = signal.signal(signal.SIGINT, lambda signum, frame: None)
        return self

    def __exit__(self, *exc: Any) -> None:
        signal.signal(signal.SIGINT, self.old)


def run_fg(args: Sequence[str], cwd: Optional[str] = None) -> int:
    """Run a command attached to the terminal (output streams live)."""
    with ChildSignals():
        try:
            proc = subprocess.Popen(list(args), cwd=cwd)
        except OSError as e:
            print(c("Could not run %s: %s" % (args[0], e), "red"))
            return 127
        return proc.wait()


def report_rc(rc: int) -> None:
    if rc == 0:
        print(c("Done.", "green"))
    elif rc in (-signal.SIGINT, 130):
        print(c("Interrupted.", "yellow"))
    else:
        print(c("Command failed (exit code %d)." % rc, "red"))


def pager_argv(at_end: bool = False, title: str = "") -> Optional[List[str]]:
    less = shutil.which("less")
    if not less or not sys.stdout.isatty():
        return None
    prompt = re.sub(r"([?:.%\\])", r"\\\1", title[:60]) + (" - " if title else "") + "q closes, arrows/PgUp/PgDn scroll"
    return [less, "-R", "-Ps" + prompt] + (["+G"] if at_end else [])


def show_text(text: str, title: Optional[str] = None) -> None:
    """Show text, through `less` when it will not fit on the screen."""
    body = strip_controls(((title + "\n" + "=" * min(len(title), 60) + "\n\n") if title else "") + text.rstrip() + "\n")
    _, rows = term_size()
    pager = pager_argv(title=title or "")
    if pager and body.count("\n") > rows - 2:
        with ChildSignals():
            try:
                proc = subprocess.Popen(pager, stdin=subprocess.PIPE, text=True, errors="replace")
                proc.communicate(body)
            except OSError:
                print(body)
                pause()
        return
    clear()
    print(body)
    pause()


def page_command(args: Sequence[str], cwd: Optional[str] = None, at_end: bool = False,
                 title: str = "") -> int:
    """Stream a command's output into the pager without holding it in memory."""
    pager = pager_argv(at_end, title)
    if not pager:
        rc = run_fg(args, cwd)
        report_rc(rc)
        pause()
        return rc
    with ChildSignals():
        try:
            producer = subprocess.Popen(list(args), cwd=cwd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError as e:
            print(c("Could not run %s: %s" % (args[0], e), "red"))
            pause()
            return 127
        try:
            viewer = subprocess.Popen(pager, stdin=producer.stdout)
        finally:
            if producer.stdout:
                producer.stdout.close()
        viewer.wait()
        if producer.poll() is None:
            producer.terminate()
            try:
                producer.wait(5)
            except subprocess.TimeoutExpired:
                producer.kill()
                producer.wait()
        return producer.returncode


# --------------------------------------------------------------------------- #
# Docker access
# --------------------------------------------------------------------------- #

def docker(*args: str, timeout: float = 60) -> Result:
    return run(("docker",) + args, timeout=timeout)


def parse_json_array(text: str) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text.startswith("["):
        return []
    try:
        value = json.loads(text)
    except ValueError:
        return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def parse_json_lines(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, dict))
    return out


def inspect_many(kind: Sequence[str], ids: Sequence[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Batch `docker <kind> inspect`. Objects that vanished in the meantime are skipped."""
    items: List[Dict[str, Any]] = []
    errors: List[str] = []
    ids = list(ids)
    for i in range(0, len(ids), 100):
        r = docker(*kind, *ids[i:i + 100])
        found = parse_json_array(r.out)
        items.extend(found)
        if not r.ok and not found:
            errors.append(r.message())
    return items, ("; ".join(errors) if errors else None)


# ---- endpoint policy ------------------------------------------------------- #

@dataclass
class Endpoint:
    context: str
    host: str
    problem: Optional[str] = None


def resolve_endpoint(env: Optional[Dict[str, str]] = None,
                     runner: Callable[..., Result] = docker,
                     is_socket: Optional[Callable[[str], bool]] = None) -> Endpoint:
    """Work out which daemon the Docker CLI would talk to; accept only a local Unix socket."""
    env = os.environ if env is None else env
    is_socket = is_socket or _is_socket
    host_env = env.get("DOCKER_HOST", "").strip()
    ctx_env = env.get("DOCKER_CONTEXT", "").strip()

    if host_env and ctx_env and ctx_env != "default":
        return Endpoint(ctx_env, host_env,
                        "Both DOCKER_HOST and DOCKER_CONTEXT are set, so the target daemon is "
                        "ambiguous. Unset one of them.")
    if host_env:
        context, host = "(DOCKER_HOST)", host_env
    else:
        r = runner("context", "show", timeout=15)
        if not r.ok or not r.out.strip():
            return Endpoint("?", "?", "Could not determine the Docker context: " + r.message())
        context = r.out.strip().splitlines()[0]
        r = runner("context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}",
                   timeout=15)
        try:
            host = json.loads(r.out.strip()) if r.ok else ""
        except ValueError:
            host = ""
        if not isinstance(host, str) or not host:
            return Endpoint(context, "?", "Could not read the endpoint of Docker context '%s'." % context)

    if not host.startswith("unix://"):
        return Endpoint(context, host,
                        "dman only manages the local Docker daemon, but the CLI points at %s. "
                        "Switch back with `docker context use default` and/or unset DOCKER_HOST." % host)
    path = host[len("unix://"):]
    if not path or not is_socket(path):
        return Endpoint(context, host, "The Docker socket %s does not exist or is not a socket. "
                                       "Is the Docker service running?" % (path or "?"))
    return Endpoint(context, host)


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


# ---- containers ------------------------------------------------------------ #

@dataclass
class Container:
    id: str
    name: str
    image: str
    image_id: str
    state: str
    health: Optional[str]
    exit_code: int
    created: Optional[datetime]
    started: Optional[datetime]
    finished: Optional[datetime]
    ports: List[str]
    labels: Dict[str, str]
    mounts: List[Dict[str, Any]]
    env: List[str]
    restart: str
    networks: List[str]
    restart_count: int = 0           # restarts by the restart policy since the last manual start
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def project(self) -> str:
        return self.labels.get(L_PROJECT, "")

    @property
    def service(self) -> str:
        return self.labels.get(L_SERVICE, "")

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def stopped(self) -> bool:
        return self.state in ("exited", "created", "dead")

    def state_text(self) -> str:
        text = self.state
        if self.state in ("exited", "dead"):
            text += " (%d)" % self.exit_code
        if self.health:
            text += " " + self.health
        if self.restart_count:
            text += " (%d restart%s)" % (self.restart_count, "" if self.restart_count == 1 else "s")
        return text

    def since(self) -> str:
        if self.running:
            return "up " + ago(self.started)
        if self.finished:
            return ago(self.finished) + " ago"
        return "made " + ago(self.created) + " ago"

    def label(self) -> str:
        return "%s (%s)" % (self.name, self.short_id)


def format_ports(ports: Optional[Dict[str, Any]]) -> List[str]:
    """{'80/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8080'}]} -> ['8080->80']"""
    out: List[str] = []

    def sort_key(item: Tuple[str, Any]) -> Tuple[int, str]:
        num = item[0].split("/")[0]
        return (int(num) if num.isdecimal() else 0, item[0])

    for cport, binds in sorted((ports or {}).items(), key=sort_key):
        if not binds:
            continue
        num, _, proto = cport.partition("/")
        for b in binds:
            ip = (b or {}).get("HostIp", "")
            hp = (b or {}).get("HostPort", "")
            text = ("" if ip in ("", "0.0.0.0", "::") else (("[%s]" % ip if ":" in ip else ip) + ":"))
            text += "%s->%s" % (hp, num) + ("" if proto in ("", "tcp") else "/" + proto)
            if text not in out:
                out.append(text)
    return out


def container_from_inspect(d: Dict[str, Any]) -> Container:
    st = d.get("State") or {}
    cfg = d.get("Config") or {}
    net = d.get("NetworkSettings") or {}
    host = d.get("HostConfig") or {}
    health = (st.get("Health") or {}).get("Status") or None
    return Container(
        id=d.get("Id", ""),
        name=(d.get("Name") or "").lstrip("/"),
        image=cfg.get("Image") or d.get("Image", ""),
        image_id=d.get("Image", ""),
        state=st.get("Status", "unknown"),
        health=health,
        exit_code=int(st.get("ExitCode") or 0),
        created=parse_ts(d.get("Created")),
        started=parse_ts(st.get("StartedAt")),
        finished=parse_ts(st.get("FinishedAt")),
        ports=format_ports(net.get("Ports")),
        labels=cfg.get("Labels") or {},
        mounts=d.get("Mounts") or [],
        env=cfg.get("Env") or [],
        restart=(host.get("RestartPolicy") or {}).get("Name") or "no",
        networks=sorted((net.get("Networks") or {}).keys()),
        restart_count=int(d.get("RestartCount") or 0),
        raw=d,
    )


def list_containers() -> Tuple[List[Container], Optional[str]]:
    r = docker("ps", "-aq", "--no-trunc")
    if not r.ok:
        return [], r.message()
    ids = r.out.split()
    if not ids:
        return [], None
    raw, err = inspect_many(("container", "inspect"), ids)
    return [container_from_inspect(d) for d in raw], err


def get_container(cid: str) -> Optional[Container]:
    found = parse_json_array(docker("container", "inspect", cid).out)
    return container_from_inspect(found[0]) if found else None


# ---- images ---------------------------------------------------------------- #

@dataclass
class Image:
    id: str
    tags: List[str]
    created: Optional[datetime]
    size: Optional[int]
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def short_id(self) -> str:
        return self.id.split(":")[-1][:12]

    @property
    def name(self) -> str:
        if not self.tags:
            return "<none>"
        return self.tags[0] + ("  (+%d)" % (len(self.tags) - 1) if len(self.tags) > 1 else "")


def list_images() -> Tuple[List[Image], Optional[str]]:
    r = docker("image", "ls", "-q", "--no-trunc")
    if not r.ok:
        return [], r.message()
    ids = list(dict.fromkeys(r.out.split()))
    if not ids:
        return [], None
    raw, err = inspect_many(("image", "inspect"), ids)
    images = [Image(id=d.get("Id", ""), tags=[t for t in (d.get("RepoTags") or []) if t != "<none>:<none>"],
                    created=parse_ts(d.get("Created")), size=d.get("Size"), raw=d) for d in raw]
    return images, err


def image_users(image: Image, containers: Sequence[Container]) -> List[Container]:
    return [ct for ct in containers if ct.image_id == image.id]


# ---- volumes --------------------------------------------------------------- #

@dataclass
class Volume:
    name: str
    driver: str
    mountpoint: str
    created: Optional[datetime]
    labels: Dict[str, str]
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def project(self) -> str:
        return self.labels.get(L_PROJECT, "")


def classify_volume(name: str, labels: Optional[Dict[str, str]]) -> str:
    """named / anonymous / unknown. Anonymous volumes always get 64-hex names, so a
    non-hex name is reliably named; a hex name without the engine's label is unknown."""
    labels = labels or {}
    if L_ANON in labels:
        return "anonymous"
    if L_CVOLUME in labels:
        return "named"
    if re.fullmatch(r"[0-9a-f]{64}", name):
        return "unknown"
    return "named"


def list_volumes() -> Tuple[List[Volume], Optional[str]]:
    r = docker("volume", "ls", "-q")
    if not r.ok:
        return [], r.message()
    names = r.out.split()
    if not names:
        return [], None
    raw, err = inspect_many(("volume", "inspect"), names)
    vols = [Volume(name=d.get("Name", ""), driver=d.get("Driver", ""), mountpoint=d.get("Mountpoint", ""),
                   created=parse_ts(d.get("CreatedAt")), labels=d.get("Labels") or {}, raw=d) for d in raw]
    return vols, err


@dataclass
class Usage:
    container: Container
    destination: str
    rw: bool


@dataclass
class MountRow:
    kind: str                 # "volume" or "bind"
    name: str                 # volume name or host source path
    vtype: str                # named / anonymous / unknown / "-"
    projects: List[str]
    users: List[Usage]
    path: str
    volume: Optional[Volume] = None


def build_mount_rows(volumes: Sequence[Volume], containers: Sequence[Container]) -> List[MountRow]:
    by_volume: Dict[str, List[Usage]] = {}
    binds: Dict[str, List[Usage]] = {}
    for ct in containers:
        for m in ct.mounts:
            use = Usage(ct, m.get("Destination", ""), bool(m.get("RW", True)))
            if m.get("Type") == "volume" and m.get("Name"):
                by_volume.setdefault(m["Name"], []).append(use)
            elif m.get("Type") == "bind" and m.get("Source"):
                binds.setdefault(m["Source"], []).append(use)
    rows = []
    for v in volumes:
        users = by_volume.get(v.name, [])
        projects = sorted({v.project} if v.project else {u.container.project for u in users if u.container.project})
        rows.append(MountRow("volume", v.name, classify_volume(v.name, v.labels), projects, users, v.mountpoint, v))
    for src, users in binds.items():
        projects = sorted({u.container.project for u in users if u.container.project})
        rows.append(MountRow("bind", src, "-", projects, users, src, None))
    return rows


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    path: Optional[str]
    roots: List[str]
    depth: int
    page_size: Optional[int]
    warnings: List[str] = field(default_factory=list)


def config_candidates(explicit: Optional[str]) -> List[str]:
    if explicit:
        return [explicit]
    env = os.environ.get("DMAN_CONFIG")
    if env:
        return [env]
    return [USER_CONFIG, SYSTEM_CONFIG]


def load_config(explicit: Optional[str] = None) -> Config:
    warnings: List[str] = []
    for path in config_candidates(explicit):
        if not os.path.exists(path):
            if explicit or os.environ.get("DMAN_CONFIG"):
                warnings.append("Config file %s does not exist." % path)
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with open(path, encoding="utf-8") as fh:
                parser.read_file(fh)
        except (OSError, configparser.Error) as e:
            warnings.append("Could not read %s: %s" % (path, e))
            return Config(path, [], DEFAULT_DEPTH, None, warnings)
        return parse_config(parser, path, warnings)
    return Config(None, [], DEFAULT_DEPTH, None, warnings)


def parse_config(parser: configparser.ConfigParser, path: Optional[str], warnings: List[str]) -> Config:
    roots: List[str] = []
    for line in parser.get("projects", "roots", fallback="").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            p = os.path.abspath(os.path.expandvars(os.path.expanduser(line)))
            if p not in roots:
                roots.append(p)
    depth = DEFAULT_DEPTH
    raw_depth = parser.get("projects", "depth", fallback=str(DEFAULT_DEPTH)).strip()
    if raw_depth.isdecimal():
        depth = min(int(raw_depth), MAX_DEPTH)
    else:
        warnings.append("depth = %r is not a number; using %d." % (raw_depth, DEFAULT_DEPTH))
    page_size = None
    raw_ps = parser.get("ui", "page_size", fallback="auto").strip().lower()
    if raw_ps.isdecimal() and int(raw_ps) > 0:
        page_size = int(raw_ps)
    elif raw_ps != "auto":
        warnings.append("page_size = %r is not a number or 'auto'." % raw_ps)
    return Config(path, roots, depth, page_size, warnings)


CFG = Config(None, [], DEFAULT_DEPTH, None)


# --------------------------------------------------------------------------- #
# Compose projects
# --------------------------------------------------------------------------- #

@dataclass
class Folder:
    path: str
    files: List[str]          # compose file names found in this folder


def scan_folders(roots: Sequence[str], depth: int) -> Tuple[List[Folder], List[str]]:
    """Find folders holding compose files, from each root down `depth` levels.
    Hidden folders are skipped and symlink loops are ignored."""
    found: List[Folder] = []
    notes: List[str] = []
    seen = set()

    def walk(path: str, level: int) -> None:
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as e:
            notes.append("cannot read %s: %s" % (path, e.strerror or e))
            return
        files = sorted(e.name for e in entries if COMPOSE_FILE_RE.match(e.name) and _is_file(e))
        if files:
            found.append(Folder(path, files))
        if level >= depth:
            return
        for e in sorted(entries, key=lambda x: x.name):
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            try:
                if e.is_dir():
                    walk(e.path, level + 1)
            except OSError:
                continue

    for root in roots:
        if not os.path.isdir(root):
            notes.append("project folder not found: %s" % root)
            continue
        walk(root, 0)
    return found, notes


def _is_file(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def split_compose_name(name: str) -> Tuple[str, Optional[str]]:
    m = COMPOSE_FILE_RE.match(name)
    if not m:
        return "", None
    return m.group("base"), m.group("mid")


def default_file_set(names: Sequence[str]) -> Optional[List[str]]:
    """The files `docker compose` would pick on its own in this folder: the first standard
    file plus its override file. If there is no standard file and exactly one other compose
    file, use that. Otherwise the choice is ambiguous (None)."""
    names = list(names)
    std = next((n for n in COMPOSE_STANDARD if n in names), None)
    if std:
        base = split_compose_name(std)[0]
        override = next((n for n in (base + ".override.yaml", base + ".override.yml") if n in names), None)
        return [std] + ([override] if override else [])
    return [names[0]] if len(names) == 1 else None


def normalize_project_name(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", text.lower()).lstrip("_-") or "project"


@dataclass
class Project:
    key: str
    name: str
    name_known: bool                 # True when the name comes from running/stopped containers
    directory: Optional[str]
    folder_files: List[str]          # compose files found in the folder (names)
    files: Optional[List[str]]       # absolute paths passed with -f; None if not determinable
    files_how: str                   # "deployed", "default", "chosen" or ""
    containers: List[Container]
    problems: List[str] = field(default_factory=list)

    @property
    def running(self) -> int:
        return sum(1 for ct in self.containers if ct.running)

    @property
    def status(self) -> str:
        if not self.containers:
            return "not deployed" if self.files else "pick files"
        if self.running == len(self.containers):
            return "running"
        return "partial" if self.running else "stopped"

    @property
    def status_text(self) -> str:
        return self.status + (" (no file)" if self.containers and not self.files else "")

    @property
    def directory_ok(self) -> bool:
        return bool(self.directory) and os.path.isdir(self.directory or "")

    @property
    def can_up(self) -> bool:
        return bool(self.files)

    def services(self) -> Dict[str, List[Container]]:
        out: Dict[str, List[Container]] = {}
        for ct in sorted(self.containers, key=lambda x: (x.service, x.name)):
            out.setdefault(ct.service or "?", []).append(ct)
        return out


# Compose files picked by hand for a folder during this session (key -> file paths).
CHOSEN_FILES: Dict[str, List[str]] = {}


def build_projects(containers: Sequence[Container], cfg: Config) -> Tuple[List[Project], List[str]]:
    folders, notes = scan_folders(cfg.roots, cfg.depth)
    by_real = {os.path.realpath(f.path): f for f in folders}
    used = set()
    grouped: Dict[str, List[Container]] = {}
    for ct in containers:
        if ct.project:
            grouped.setdefault(ct.project, []).append(ct)

    projects: List[Project] = []
    for name, cts in grouped.items():
        newest = max(cts, key=lambda x: x.created or datetime.min.replace(tzinfo=timezone.utc))
        workdir = newest.labels.get(L_WORKDIR) or next((x.labels.get(L_WORKDIR) for x in cts if x.labels.get(L_WORKDIR)), "")
        label_files = [f for f in (newest.labels.get(L_FILES) or "").split(",") if f]
        folder = by_real.get(os.path.realpath(workdir)) if workdir else None
        p = Project(key="project:" + name, name=name, name_known=True, directory=workdir or None,
                    folder_files=folder.files if folder else [], files=None, files_how="", containers=cts)
        if folder:
            used.add(folder.path)
        if p.key in CHOSEN_FILES:
            p.files, p.files_how = CHOSEN_FILES[p.key], "chosen"
        elif label_files and all(os.path.isfile(f) for f in label_files):
            p.files, p.files_how = label_files, "deployed"
        else:
            if label_files:
                missing = [f for f in label_files if not os.path.isfile(f)]
                p.problems.append("compose file(s) used at deploy time are missing: " + ", ".join(missing))
            if not workdir:
                p.problems.append("containers carry no Compose working-directory label")
            if folder:
                default = default_file_set(folder.files)
                if default:
                    p.files, p.files_how = [os.path.join(folder.path, n) for n in default], "default"
        projects.append(p)

    for folder in folders:
        if folder.path in used:
            continue
        key = "folder:" + os.path.realpath(folder.path)
        p = Project(key=key, name=normalize_project_name(os.path.basename(folder.path.rstrip("/"))),
                    name_known=False, directory=folder.path, folder_files=folder.files,
                    files=None, files_how="", containers=[])
        if key in CHOSEN_FILES:
            p.files, p.files_how = CHOSEN_FILES[key], "chosen"
        else:
            default = default_file_set(folder.files)
            if default:
                p.files, p.files_how = [os.path.join(folder.path, n) for n in default], "default"
            else:
                p.problems.append("several compose files and no standard one; choose which to use")
        projects.append(p)
    return projects, notes


def load_projects() -> Tuple[List[Project], Optional[str], List[str]]:
    containers, err = list_containers()
    projects, notes = build_projects(containers, CFG)
    return projects, err, notes


def find_project(key: str, directory: Optional[str] = None) -> Optional[Project]:
    """Find a project again by key; a project moves between 'folder:' and 'project:' keys
    when it is deployed or taken down, so fall back to its folder."""
    projects, _, _ = load_projects()
    found = next((p for p in projects if p.key == key), None)
    if found is None and directory:
        real = os.path.realpath(directory)
        matches = [p for p in projects if p.directory and os.path.realpath(p.directory) == real]
        if len(matches) == 1:
            found = matches[0]
    return found


def compose_base(p: Project) -> List[str]:
    args = ["docker", "compose"]
    if p.directory_ok:
        args += ["--project-directory", p.directory or ""]
    for f in p.files or []:
        args += ["-f", f]
    if p.name_known:
        args += ["-p", p.name]
    return args


def compose_cwd(p: Project) -> Optional[str]:
    return p.directory if p.directory_ok else None


def compose_config_json(p: Project) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not p.files:
        return None, "no compose files"
    r = run(compose_base(p) + ["config", "--format", "json"], cwd=compose_cwd(p), timeout=60)
    if not r.ok:
        return None, r.message()
    try:
        data = json.loads(r.out)
        return (data, None) if isinstance(data, dict) else (None, "unexpected output")
    except ValueError:
        return None, "could not parse `docker compose config` output"


# --------------------------------------------------------------------------- #
# Generic list screen
# --------------------------------------------------------------------------- #

@dataclass
class Col:
    title: str
    get: Callable[[Any], str]
    max: int = 30
    min: int = 4
    flex: bool = False
    drop: int = 0                                   # higher = hidden first on narrow terminals
    style: Optional[Callable[[Any], Sequence[str]]] = None
    want: int = 0                                   # width to keep before hiding other columns


def layout_columns(cols: Sequence[Col], cells: List[List[str]], avail: int) -> List[Tuple[int, int]]:
    """Return (column index, width) for the columns that fit in `avail` characters."""
    active = list(range(len(cols)))
    widths = {}
    for i, col in enumerate(cols):
        natural = max([len(col.title)] + [len(row[i]) for row in cells])
        widths[i] = max(min(natural, col.max), min(len(col.title), col.max))

    def total() -> int:
        return sum(widths[i] for i in active) + 2 * (len(active) - 1)

    def shrink(floor: Callable[[int], int]) -> bool:
        """Narrow the widest flexible column toward its floor. False when nothing can give."""
        shrinkable = [i for i in active if cols[i].flex and widths[i] > floor(i)]
        if not shrinkable:
            return False
        i = max(shrinkable, key=lambda k: widths[k] - floor(k))
        widths[i] = max(floor(i), widths[i] - (total() - avail))
        return True

    soft = lambda i: max(cols[i].min, cols[i].want)
    while total() > avail:
        # 1) trim flexible columns to a comfortable width, 2) hide optional columns,
        # 3) trim further down to the hard minimum.
        if shrink(soft):
            continue
        droppable = [i for i in active if cols[i].drop > 0]
        if droppable:
            active.remove(max(droppable, key=lambda k: (cols[k].drop, k)))
            continue
        if not shrink(lambda i: cols[i].min):
            break
    return [(i, widths[i]) for i in active]


class ListView:
    def __init__(self, title: str, fetch: Callable[[], Tuple[List[Any], Optional[str]]],
                 cols: Sequence[Col], *, sorts: Optional[List[Tuple[str, Callable[[List[Any]], List[Any]]]]] = None,
                 search: Optional[Callable[[Any], str]] = None,
                 on_select: Optional[Callable[[Any], None]] = None,
                 help_text: str = "", empty: str = "Nothing to show.",
                 keys: Optional[Dict[str, Tuple[str, Callable[["ListView", List[Any]], None]]]] = None,
                 notes: Optional[Callable[[], List[str]]] = None,
                 auto_refresh: float = 0):
        self.title, self.fetch, self.cols = title, fetch, list(cols)
        self.sorts = sorts or []
        self.search, self.on_select = search, on_select
        self.help_text, self.empty = help_text, empty
        self.keys = keys or {}
        self.notes = notes
        self.auto_refresh = auto_refresh
        self.page = 0
        self.sort_idx = 0
        self.filter = ""
        self.message: Optional[str] = None
        self.items: List[Any] = []
        self.error: Optional[str] = None

    def page_size(self) -> int:
        if CFG.page_size:
            return CFG.page_size
        _, rows = term_size()
        return max(3, min(40, rows - 9))

    def visible(self) -> List[Any]:
        view = self.items
        if self.filter and self.search:
            words = self.filter.lower().split()
            view = [it for it in view if all(w in self.search(it).lower() for w in words)]
        if self.sorts:
            view = self.sorts[self.sort_idx % len(self.sorts)][1](list(view))
        return view

    def load(self) -> None:
        self.items, self.error = self.fetch()

    def render(self, live: bool = False) -> List[Any]:
        view = self.visible()
        size = self.page_size()
        pages = max(1, -(-len(view) // size))
        self.page = min(self.page, pages - 1)
        page_items = view[self.page * size:(self.page + 1) * size]
        cols_avail, _ = term_size()

        clear()
        head = self.title
        extra = []
        if len(view) != len(self.items):
            extra.append("%d of %d shown" % (len(view), len(self.items)))
        else:
            extra.append("%d" % len(self.items))
        if pages > 1:
            extra.append("page %d/%d" % (self.page + 1, pages))
        if self.sorts:
            extra.append("sort: " + self.sorts[self.sort_idx % len(self.sorts)][0])
        if self.filter:
            extra.append("filter: " + self.filter)
        print(c(head, "bold", "cyan") + c("  " + " · ".join(extra), "dim"))
        for note in (self.notes() if self.notes else []):
            print(c(fit(note, cols_avail), "yellow"))
        if self.error:
            print(c(fit("Docker reported: " + self.error.splitlines()[0], cols_avail), "red"))
        print()

        if not page_items:
            print("  " + (self.empty if not self.filter else "Nothing matches the filter."))
        else:
            num_w = len(str(len(page_items)))
            cells = [[col.get(it) for col in self.cols] for it in page_items]
            plan = layout_columns(self.cols, cells, cols_avail - num_w - 3)
            header = " " * (num_w + 2) + "  ".join(fit(self.cols[i].title, w).ljust(w) for i, w in plan)
            print(c(header.rstrip(), "dim"))
            for n, (it, row) in enumerate(zip(page_items, cells), 1):
                parts = []
                for i, w in plan:
                    text = fit(row[i], w).ljust(w)
                    styles = self.cols[i].style(it) if self.cols[i].style else ()
                    parts.append(c(text, *styles))
                print(c(str(n).rjust(num_w), "bold") + "  " + "  ".join(parts).rstrip())
        print()
        if self.message:
            print(self.message)
            self.message = None
        if live:
            print(c("Refreshing every %ds - Ctrl-C to stop" % self.auto_refresh, "dim"))
        else:
            keys = []
            if page_items and self.on_select:
                keys.append("1-%d open" % len(page_items))
            if pages > 1:
                keys.append("n/p page")
            if len(self.sorts) > 1:
                keys.append("s sort")
            if self.search:
                keys.append("f filter")
            for k, (label, _) in self.keys.items():
                keys.append("%s %s" % (k, label))
            if self.auto_refresh:
                keys.append("a auto-refresh")
            keys += ["Enter refresh", "h help", "b back", "q quit"]
            print(c(" · ".join(keys), "dim"))
        return page_items

    def run(self) -> None:
        while True:
            try:
                self.load()
            except KeyboardInterrupt:
                return
            page_items = self.render()
            key = ask("> ")
            low = key.lower()
            if low == "":
                continue
            if low.isdecimal():
                n = int(low)
                if self.on_select and 1 <= n <= len(page_items):
                    self.on_select(page_items[n - 1])
                else:
                    self.message = c("No item %s on this page." % key, "yellow")
            elif low == "n":
                self.page += 1
            elif low == "p":
                self.page = max(0, self.page - 1)
            elif low == "s" and len(self.sorts) > 1:
                self.choose_sort()
            elif low == "f" and self.search:
                self.filter = ask("Filter (words to match, blank clears): ", on_interrupt=self.filter)
                self.page = 0
            elif low == "a" and self.auto_refresh:
                self.live()
            elif low in self.keys:
                self.keys[low][1](self, self.visible())
            elif low == "h":
                show_text(self.help_text or LIST_HELP, self.title + " - help")
            elif low == "b":
                return
            elif low == "q":
                raise QuitApp()
            else:
                self.message = c("Unknown key: %s" % key, "yellow")

    def choose_sort(self) -> None:
        print()
        for i, (label, _) in enumerate(self.sorts, 1):
            print("  %d  %s" % (i, label))
        pick = ask("Sort by: ")
        if pick.isdecimal() and 1 <= int(pick) <= len(self.sorts):
            self.sort_idx = int(pick) - 1
            self.page = 0

    def live(self) -> None:
        try:
            while True:
                self.load()
                self.render(live=True)
                time.sleep(self.auto_refresh)
        except KeyboardInterrupt:
            pass


def menu(header: Sequence[str], options: Sequence[Tuple[str, str, str]], message: Optional[str] = None) -> str:
    """Show a header and numbered options (key, label, style). Returns the key typed."""
    clear()
    for line in header:
        print(line)
    print()
    cols, _ = term_size()
    width = max(len(k) for k, _, _ in options) if options else 1
    cells = ["%s  %s" % (k.rjust(width), label) for k, label, _ in options]
    styles = [s for _, _, s in options]
    col_w = max(len(x) for x in cells) + 3 if cells else 0
    ncols = 2 if cols >= 2 * col_w and len(cells) > 6 else 1
    nrows = -(-len(cells) // ncols)
    for r in range(nrows):
        line = ""
        for k in range(ncols):
            i = r + k * nrows
            if i < len(cells):
                text = fit(cells[i], cols - 1) if ncols == 1 else cells[i].ljust(col_w)
                line += c(text, styles[i]) if styles[i] else text
        print(line.rstrip())
    print(c("b back · q quit", "dim"))
    if message:
        print()
        print(message)
    key = ask("> ").lower()
    if key == "q":
        raise QuitApp()
    return key


def redacted_lines(pairs: Sequence[Tuple[str, str]], reveal: bool) -> List[str]:
    lines = []
    for key, value in pairs:
        if reveal:
            lines.append("%s=%s" % (key, value))
        else:
            lines.append("%s=%s" % (key, "<hidden>" if value else "<empty>"))
    return lines


def show_env(title: str, sections: Sequence[Tuple[str, List[Tuple[str, str]]]], note: str = "") -> None:
    reveal = False
    while True:
        clear()
        title_line(title)
        if note:
            print(c(note, "dim"))
        print(c("Values are shown." if reveal else "Values are hidden.", "yellow"))
        for heading, pairs in sections:
            print()
            print(c(heading, "bold"))
            if not pairs:
                print("  (none)")
            for line in redacted_lines(pairs, reveal):
                print("  " + line)
        print()
        key = ask(c("r reveal values · Enter/b back > " if not reveal else "Enter/b back > ", "dim")).lower()
        if key == "r" and not reveal:
            print(c("Values may include passwords and tokens. They will appear in this terminal "
                    "and its scrollback (and any session recording).", "yellow"))
            reveal = confirm("Reveal values?")
        elif key == "q":
            raise QuitApp()
        else:
            return


def split_env(entries: Sequence[str]) -> List[Tuple[str, str]]:
    out = []
    for e in entries:
        k, _, v = e.partition("=")
        out.append((k, v))
    return out


def read_dotenv(path: str) -> Tuple[Optional[List[Tuple[str, str]]], Optional[str]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, "cannot read %s: %s" % (path, e.strerror or e)
    pairs = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        k, sep, v = s.partition("=")
        if sep:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            pairs.append((k.strip(), v))
    return pairs, None


# --------------------------------------------------------------------------- #
# Screens: containers
# --------------------------------------------------------------------------- #

def state_style(ct: Container) -> Sequence[str]:
    if ct.health == "unhealthy" or ct.state in ("dead", "restarting"):
        return ("red",)
    if ct.running:
        return ("yellow",) if ct.restart_count else ("green",)
    if ct.state == "paused":
        return ("yellow",)
    return ("dim",)


def container_cols() -> List[Col]:
    return [
        Col("NAME", lambda ct: ct.name, max=32, min=8, flex=True, want=16),
        Col("STATE", lambda ct: ct.state_text(), max=22, min=7, flex=True, style=state_style, want=9),
        Col("PORTS", lambda ct: ", ".join(ct.ports) or "-", max=40, min=6, flex=True, want=12),
        Col("PROJECT/SERVICE", lambda ct: ("%s/%s" % (ct.project, ct.service)) if ct.project else "-",
            max=30, min=8, flex=True, drop=2),
        Col("AGE", lambda ct: ct.since(), max=12, min=4, drop=3),
    ]


def container_sorts() -> List[Tuple[str, Callable[[List[Container]], List[Container]]]]:
    return [
        ("running first", lambda xs: sorted(xs, key=lambda x: (not x.running, x.name))),
        ("name", lambda xs: sorted(xs, key=lambda x: x.name)),
        ("project", lambda xs: sorted(xs, key=lambda x: (x.project == "", x.project, x.service, x.name))),
        ("newest", lambda xs: sorted(xs, key=lambda x: x.created or datetime.min.replace(tzinfo=timezone.utc), reverse=True)),
    ]


def container_search(ct: Container) -> str:
    return " ".join([ct.name, ct.short_id, ct.image, ct.state_text(), ct.project, ct.service] + ct.ports)


def containers_screen(project: Optional[str] = None) -> None:
    def fetch() -> Tuple[List[Container], Optional[str]]:
        cts, err = list_containers()
        if project is not None:
            cts = [ct for ct in cts if ct.project == project]
        return cts, err

    title = "Containers" + (" in project %s" % project if project else "")
    ListView(title, fetch, container_cols(), sorts=container_sorts(), search=container_search,
             on_select=lambda ct: container_view(ct.id), help_text=CONTAINERS_HELP,
             empty="No containers.").run()


def container_header(ct: Container) -> List[str]:
    lines = [c(ct.name, "bold", "cyan") + c("  " + ct.short_id, "dim"),
             "State:   " + c(ct.state_text(), *state_style(ct)) + "  " + c(ct.since(), "dim"),
             "Image:   " + ct.image,
             "Ports:   " + (", ".join(ct.ports) or "-")]
    if ct.project:
        lines.append("Compose: %s / %s" % (ct.project, ct.service))
    return lines


def container_view(cid: str) -> None:
    message = None
    while True:
        ct = get_container(cid)
        if ct is None:
            print(c("That container no longer exists.", "yellow"))
            pause()
            return
        opts = [("1", "Logs", "")]
        if not ct.running and ct.state != "paused":
            opts.append(("2", "Start", ""))
        if ct.running:
            opts.append(("3", "Stop", ""))
        opts.append(("4", "Restart", ""))
        if ct.running:
            opts += [("5", "Shell", ""), ("6", "Run a command", "")]
        opts += [("7", "Details", ""), ("8", "Environment", ""), ("9", "Mounts / volumes", "")]
        if ct.running:
            opts.append(("10", "Resource usage", ""))
        if ct.project:
            opts.append(("11", "Compose project: " + ct.project, ""))
        opts += [("12", "Raw inspect (JSON)", ""), ("13", "Remove container", "red")]
        key = menu(container_header(ct), opts, message)
        message = None
        valid = {k for k, _, _ in opts}
        if key == "b":
            return
        if key == "":
            continue
        if key not in valid:
            message = c("Not available here: %s" % key, "yellow")
            continue
        if key == "1":
            logs_menu("Logs: " + ct.label(), lambda opts: ["docker", "logs"] + opts + [ct.id])
        elif key == "2":
            message = simple_action(["start", ct.id], "Starting " + ct.label())
        elif key == "3":
            if confirm("Stop %s?" % ct.label(), default=True):
                message = simple_action(["stop", ct.id], "Stopping " + ct.label(), timeout=180)
        elif key == "4":
            if confirm("Restart %s?" % ct.label(), default=True):
                message = simple_action(["restart", ct.id], "Restarting " + ct.label(), timeout=180)
        elif key == "5":
            open_shell(ct)
        elif key == "6":
            exec_command(ct)
        elif key == "7":
            show_text(container_details(ct), "Details: " + ct.label())
        elif key == "8":
            show_env("Environment: " + ct.label(), [("Container environment", split_env(ct.env))])
        elif key == "9":
            mounts_of_container(ct)
        elif key == "10":
            r = docker("stats", "--no-stream", ct.id, timeout=30)
            show_text(r.out if r.ok else r.message(), "Resource usage (current snapshot): " + ct.label())
        elif key == "11":
            p = next((x for x in load_projects()[0] if x.name_known and x.name == ct.project), None)
            if p:
                project_view(p.key)
            else:
                message = c("Project not found.", "yellow")
        elif key == "12":
            print(c("Raw inspect output includes environment values, which may be secrets.", "yellow"))
            if confirm("Show it?"):
                r = docker("container", "inspect", ct.id)
                show_text(r.out if r.ok else r.message(), "Inspect: " + ct.label())
        elif key == "13":
            if remove_container(ct):
                return


def simple_action(args: List[str], doing: str, timeout: float = 60) -> str:
    print(doing + "...")
    r = docker(*args, timeout=timeout)
    return c("Done.", "green") if r.ok else c("Failed: " + r.message(), "red")


LOG_SINCE_RE = re.compile(r"(\d+(\.\d+)?(ns|us|ms|s|m|h))+|\d{4}-\d\d-\d\d([T ]\d\d:\d\d(:\d\d)?)?\S*|\d{9,}")


def log_options(tail: Optional[str] = None, follow: bool = False, since: Optional[str] = None,
                timestamps: bool = False) -> List[str]:
    """Options shared by `docker logs` and `docker compose logs`."""
    return ((["-f"] if follow else []) + (["-t"] if timestamps else [])
            + (["--since", since] if since else []) + (["--tail", tail] if tail else []))


def logs_menu(title: str, build: Callable[[List[str]], List[str]], cwd: Optional[str] = None) -> None:
    """`build` turns log options into the full command."""
    timestamps = False
    message = None
    while True:
        key = menu([c(title, "bold", "cyan"),
                    c("What is kept depends on the Docker logging driver and its rotation settings.", "dim")],
                   [("1", "Last 100 lines", ""), ("2", "Last 500 lines", ""), ("3", "All lines", ""),
                    ("4", "Custom number of lines", ""), ("5", "Follow live (Ctrl-C to stop)", ""),
                    ("6", "Since a time (e.g. 15m, 2h, 2026-10-22)", ""),
                    ("t", "Timestamps: " + ("on" if timestamps else "off"), "")], message)
        message = None
        if key in ("b", ""):
            return
        if key == "t":
            timestamps = not timestamps
        elif key in ("1", "2", "3", "4", "6"):
            tail: Optional[str] = {"1": "100", "2": "500", "3": "all"}.get(key)
            since = None
            if key == "4":
                n = ask("How many lines? ")
                if not n.isdecimal() or int(n) < 1:
                    continue
                tail = n
            elif key == "6":
                since = ask("Since (a duration such as 15m or 2h30m, a date or date-time): ")
                if not since:
                    continue
                if not LOG_SINCE_RE.fullmatch(since):
                    message = c("Not a duration or date Docker understands: %s" % since, "yellow")
                    continue
            page_command(build(log_options(tail, since=since, timestamps=timestamps)), cwd=cwd,
                         at_end=True, title=title)
        elif key == "5":
            clear()
            print(c("Following logs. Press Ctrl-C to stop.", "dim"))
            rc = run_fg(build(log_options("100", follow=True, timestamps=timestamps)), cwd=cwd)
            print()
            if rc not in (0, -signal.SIGINT, 130):
                report_rc(rc)
            pause()


def open_shell(ct: Container) -> None:
    tty = ["-it"] if sys.stdin.isatty() else ["-i"]
    for sh in ("/bin/bash", "/bin/sh"):
        if docker("exec", ct.id, sh, "-c", "exit 0", timeout=20).ok:
            break
    else:
        print(c("Neither /bin/bash nor /bin/sh exists in %s, so there is no shell to open. "
                "Minimal images often ship without one." % ct.label(), "yellow"))
        pause()
        return
    clear()
    print(c("Opening %s in %s. Type 'exit' to come back to dman." % (sh, ct.label()), "dim"))
    run_fg(["docker", "exec"] + tty + [ct.id, sh])


def exec_command(ct: Container) -> None:
    clear()
    title_line("Run a command in " + ct.label())
    print(c("The line is split like a shell would split arguments (quotes work), but it is not "
            "run through a shell: pipes, redirects, $VARS and * are passed literally.\n"
            "For shell features, run e.g.:  sh -c 'ls /data | wc -l'", "dim"))
    print(c("A command can change or destroy data in the container and its mounted volumes.", "yellow"))
    line = ask("\nCommand: ", on_interrupt="")
    if not line:
        return
    try:
        argv = shlex.split(line)
    except ValueError as e:
        print(c("Could not parse that: %s" % e, "red"))
        pause()
        return
    print("\nWill run in %s, as %d argument(s):" % (ct.label(), len(argv)))
    for a in argv:
        print("  " + json.dumps(a))
    if not confirm("Run it?", default=True):
        return
    tty = ["-it"] if sys.stdin.isatty() else ["-i"]
    print()
    rc = run_fg(["docker", "exec"] + tty + [ct.id] + argv)
    print()
    report_rc(rc)
    pause()


def container_details(ct: Container) -> str:
    d = ct.raw
    st = d.get("State") or {}
    cfg = d.get("Config") or {}
    out = [
        "Name:        %s" % ct.name,
        "ID:          %s" % ct.id,
        "Image:       %s  (%s)" % (ct.image, ct.image_id.split(":")[-1][:12]),
        "State:       %s" % ct.state_text(),
        "Created:     %s  (%s ago)" % (local_time(ct.created), ago(ct.created)),
    ]
    if ct.started:
        out.append("Started:     %s  (%s ago)" % (local_time(ct.started), ago(ct.started)))
    if ct.finished and not ct.running:
        out.append("Finished:    %s  (%s ago)" % (local_time(ct.finished), ago(ct.finished)))
    if st.get("OOMKilled"):
        out.append("OOM killed:  yes")
    if st.get("Error"):
        out.append("Error:       %s" % st.get("Error"))
    out.append("Restart:     %s%s" % (ct.restart, "  (restarted %d times since it was last started by hand)"
                                       % ct.restart_count if ct.restart_count else ""))
    cmd = [d.get("Path", "")] + list(d.get("Args") or [])
    out.append("Command:     %s" % shlex.join([x for x in cmd if x]))
    health = st.get("Health") or {}
    if health:
        out.append("Health:      %s (failing streak %s)" % (health.get("Status"), health.get("FailingStreak", 0)))
        logs = health.get("Log") or []
        if logs:
            last = logs[-1]
            out.append("  last check exit %s: %s" % (last.get("ExitCode"), (last.get("Output") or "").strip()[:300]))
        test = (cfg.get("Healthcheck") or {}).get("Test")
        if test:
            out.append("  test: %s" % " ".join(test))
    out.append("")
    out.append("Ports:       %s" % (", ".join(ct.ports) or "-"))
    out.append("Networks:    %s" % (", ".join(ct.networks) or "-"))
    out.append("")
    out.append("Mounts:")
    if not ct.mounts:
        out.append("  (none)")
    for m in ct.mounts:
        src = m.get("Name") or m.get("Source") or "?"
        out.append("  %-6s %s -> %s%s" % (m.get("Type", "?"), src, m.get("Destination", "?"),
                                          "" if m.get("RW", True) else " (read-only)"))
    out.append("")
    if ct.project:
        out.append("Compose:")
        out.append("  project:  %s" % ct.project)
        out.append("  service:  %s" % ct.service)
        out.append("  folder:   %s" % (ct.labels.get(L_WORKDIR) or "-"))
        out.append("  files:    %s" % (ct.labels.get(L_FILES) or "-").replace(",", "\n            "))
        out.append("")
    other = {k: v for k, v in ct.labels.items() if not k.startswith("com.docker.compose.")}
    out.append("Labels:%s" % ("" if other else "      (none besides Compose labels)"))
    for k in sorted(other):
        out.append("  %s=%s" % (k, other[k]))
    out.append("")
    out.append("Environment: %d variables (see Environment; values hidden by default)" % len(ct.env))
    return "\n".join(out)


def remove_container(ct: Container) -> bool:
    clear()
    title_line("Remove container")
    print("Container: %s" % ct.label())
    print("Image:     %s" % ct.image)
    print("State:     %s" % ct.state_text())
    if ct.running:
        print(c("It is running and will be killed (docker rm -f).", "yellow"))
    if ct.project:
        print(c("This container belongs to Compose project '%s'. Compose may recreate it on the next "
                "`up`; the project's Down action is usually the better tool." % ct.project, "yellow"))
    print(c("Anonymous volumes attached to it are kept.", "dim"))
    if not confirm("\nRemove %s?" % ct.label()):
        return False
    fresh = get_container(ct.id)
    if fresh is None:
        print(c("It is already gone.", "yellow"))
        pause()
        return True
    if fresh.running != ct.running:
        print(c("Its state changed to '%s' since you reviewed it. Nothing was removed; "
                "please review again." % fresh.state, "yellow"))
        pause()
        return False
    r = docker("rm", *(["-f"] if fresh.running else []), fresh.id, timeout=120)
    print(c("Removed %s." % fresh.label(), "green") if r.ok else c("Failed: " + r.message(), "red"))
    pause()
    return r.ok


def mounts_of_container(ct: Container) -> None:
    while True:
        mounts = [m for m in ct.mounts if m.get("Type") in ("volume", "bind")]
        opts = []
        for i, m in enumerate(mounts, 1):
            src = m.get("Name") or m.get("Source") or "?"
            opts.append((str(i), "%s %s -> %s%s" % (m.get("Type"), src, m.get("Destination"),
                                                    "" if m.get("RW", True) else " (ro)"), ""))
        header = [c("Mounts of " + ct.label(), "bold", "cyan")]
        if not opts:
            header.append("This container has no volume or bind mounts.")
        key = menu(header, opts)
        if key in ("b", ""):
            return
        if key.isdecimal() and 1 <= int(key) <= len(mounts):
            m = mounts[int(key) - 1]
            if m.get("Type") == "volume":
                mount_view("volume", m.get("Name", ""))
            else:
                mount_view("bind", m.get("Source", ""))


# --------------------------------------------------------------------------- #
# Screens: live resource usage
# --------------------------------------------------------------------------- #

def _pct(text: str) -> float:
    try:
        return float(text.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return -1.0


def stats_screen() -> None:
    def fetch() -> Tuple[List[Dict[str, Any]], Optional[str]]:
        r = docker("stats", "--no-stream", "--format", "{{json .}}", timeout=45)
        rows = parse_json_lines(r.out)
        return rows, (None if r.ok else r.message())

    def val(key: str) -> Callable[[Dict[str, Any]], str]:
        return lambda row: (row.get(key) or "-").replace("--", "-") or "-"

    cols = [
        Col("NAME", val("Name"), max=30, min=8, flex=True, want=14),
        Col("CPU", val("CPUPerc"), max=8),
        Col("MEM USED / LIMIT", val("MemUsage"), max=24, min=8, flex=True),
        Col("MEM", val("MemPerc"), max=8, drop=3),
        Col("NET I/O", val("NetIO"), max=22, min=8, flex=True, drop=2),
        Col("BLOCK I/O", val("BlockIO"), max=22, min=8, flex=True, drop=1),
        Col("PIDS", val("PIDs"), max=6, drop=4),
    ]
    sorts = [("CPU", lambda xs: sorted(xs, key=lambda r: _pct(r.get("CPUPerc", "")), reverse=True)),
             ("memory", lambda xs: sorted(xs, key=lambda r: _pct(r.get("MemPerc", "")), reverse=True)),
             ("name", lambda xs: sorted(xs, key=lambda r: r.get("Name", "")))]
    ListView("Live resource usage (running containers, current values)", fetch, cols, sorts=sorts,
             search=lambda r: r.get("Name", ""), on_select=lambda r: container_view(r.get("Container") or r.get("ID", "")),
             help_text=STATS_HELP, empty="No running containers.", auto_refresh=3).run()


# --------------------------------------------------------------------------- #
# Screens: images
# --------------------------------------------------------------------------- #

def images_screen() -> None:
    state: Dict[str, List[Container]] = {"containers": []}

    def fetch() -> Tuple[List[Image], Optional[str]]:
        imgs, err = list_images()
        state["containers"], err2 = list_containers()
        return imgs, err or err2

    def used(img: Image) -> str:
        users = image_users(img, state["containers"])
        if not users:
            return "unused"
        running = sum(1 for u in users if u.running)
        return "%d (%d running)" % (len(users), running)

    cols = [
        Col("IMAGE", lambda i: i.name, max=50, min=10, flex=True, want=22),
        Col("ID", lambda i: i.short_id, max=12, drop=2),
        Col("SIZE", lambda i: human_bytes(i.size), max=9),
        Col("CREATED", lambda i: ago(i.created) + " ago", max=10, drop=3),
        Col("CONTAINERS", used, max=16, style=lambda i: () if used(i) != "unused" else ("dim",)),
    ]
    sorts = [("size", lambda xs: sorted(xs, key=lambda i: i.size or 0, reverse=True)),
             ("newest", lambda xs: sorted(xs, key=lambda i: i.created or datetime.min.replace(tzinfo=timezone.utc), reverse=True)),
             ("name", lambda xs: sorted(xs, key=lambda i: (not i.tags, i.name)))]
    ListView("Images", fetch, cols, sorts=sorts, search=lambda i: " ".join(i.tags + [i.short_id, used(i)]),
             on_select=lambda i: image_view(i.id), help_text=IMAGES_HELP, empty="No images.").run()


def get_image(iid: str) -> Optional[Image]:
    found = parse_json_array(docker("image", "inspect", iid).out)
    if not found:
        return None
    d = found[0]
    return Image(id=d.get("Id", ""), tags=[t for t in (d.get("RepoTags") or []) if t != "<none>:<none>"],
                 created=parse_ts(d.get("Created")), size=d.get("Size"), raw=d)


def image_view(iid: str) -> None:
    message = None
    while True:
        img = get_image(iid)
        if img is None:
            print(c("That image no longer exists.", "yellow"))
            pause()
            return
        users = image_users(img, list_containers()[0])
        header = [c("Image " + img.short_id, "bold", "cyan"),
                  "Tags:     " + (", ".join(img.tags) or "<none> (untagged)"),
                  "Size:     %s  (includes layers shared with other images)" % human_bytes(img.size),
                  "Created:  %s ago (when the image was built, not when it was pulled)" % ago(img.created),
                  "Used by:  " + (", ".join("%s [%s]" % (u.name, u.state) for u in users) or "no containers")]
        if not users:
            header.append(c("Unused now, but a Compose project may still need it for its next deploy.", "dim"))
        opts = []
        if img.tags:
            opts.append(("1", "Pull (refresh from registry)", ""))
        opts += [("2", "Containers using it", ""), ("3", "Raw inspect (JSON)", ""), ("4", "Remove image", "red")]
        key = menu(header, opts, message)
        message = None
        if key == "b":
            return
        if key == "1" and img.tags:
            tag = img.tags[0]
            if len(img.tags) > 1:
                pick = ask("Pull which tag? " + "  ".join("%d) %s" % (i, t) for i, t in enumerate(img.tags, 1)) + "  : ")
                if not (pick.isdecimal() and 1 <= int(pick) <= len(img.tags)):
                    continue
                tag = img.tags[int(pick) - 1]
            clear()
            rc = run_fg(["docker", "pull", tag])
            report_rc(rc)
            print(c("Running containers keep using the old image until they are recreated "
                    "(for Compose: Up or Pull & up).", "dim"))
            pause()
            if get_image(iid) is None:
                return
        elif key == "2":
            if not users:
                message = c("No containers use this image.", "dim")
            else:
                ListView("Containers using " + (img.tags[0] if img.tags else img.short_id),
                         lambda: ([ct for ct in list_containers()[0] if ct.image_id == iid], None),
                         container_cols(), sorts=container_sorts(), on_select=lambda ct: container_view(ct.id),
                         help_text=CONTAINERS_HELP).run()
        elif key == "3":
            r = docker("image", "inspect", img.id)
            show_text(r.out if r.ok else r.message(), "Inspect image " + img.short_id)
        elif key == "4":
            if remove_images([img]):
                return


def remove_images(images: List[Image], ask_first: bool = True) -> bool:
    """Remove images that no container (running or stopped) uses. Returns True if all went."""
    containers = list_containers()[0]
    blocked = [(img, image_users(img, containers)) for img in images]
    blocked = [(i, u) for i, u in blocked if u]
    if blocked:
        print()
        for img, users in blocked:
            print(c("%s (%s) is used by: %s" % (img.name, img.short_id, ", ".join(u.label() for u in users)), "yellow"))
        print("Remove those containers first. Nothing was removed.")
        pause()
        return False
    if ask_first:
        print()
        for img in images:
            print("  %s  %s  %s" % (img.short_id, img.name, human_bytes(img.size)))
        if not confirm("Remove %s?" % ("this image" if len(images) == 1 else "these %d images" % len(images))):
            return False
    ok = True
    for img in images:
        fresh = get_image(img.id)
        if fresh is None:
            print(c("%s is already gone." % img.short_id, "dim"))
            continue
        if image_users(fresh, list_containers()[0]) or sorted(fresh.tags) != sorted(img.tags):
            print(c("%s changed since you reviewed it (new tags or a container now uses it); skipped." % img.short_id, "yellow"))
            ok = False
            continue
        r = docker("rmi", *(fresh.tags or [fresh.id]), timeout=120)
        if r.ok:
            print(c("Removed %s %s" % (fresh.short_id, fresh.name), "green"))
        else:
            ok = False
            print(c("Failed %s: %s" % (fresh.short_id, r.message()), "red"))
    pause()
    return ok


# --------------------------------------------------------------------------- #
# Screens: volumes and mounts
# --------------------------------------------------------------------------- #

def volumes_screen() -> None:
    sizes: Dict[str, str] = {}

    def fetch() -> Tuple[List[MountRow], Optional[str]]:
        vols, err = list_volumes()
        cts, err2 = list_containers()
        return build_mount_rows(vols, cts), err or err2

    def users(row: MountRow) -> str:
        if not row.users:
            return "unused"
        return ", ".join(sorted({u.container.name for u in row.users}))

    def compute_sizes(view: ListView, _items: List[Any]) -> None:
        print("Asking Docker for volume sizes (this scans volume data and can take a while)...")
        r = docker("system", "df", "-v", "--format", "{{json .}}", timeout=600)
        try:
            data = json.loads(r.out) if r.ok else {}
        except ValueError:
            data = {}
        for v in (data.get("Volumes") or []) if isinstance(data, dict) else []:
            sizes[v.get("Name", "")] = v.get("Size") or "?"
        view.message = (c("Sizes updated.", "green") if sizes else
                        c("Docker did not report sizes: " + (r.message() if not r.ok else "no data"), "yellow"))

    def size(row: MountRow) -> str:
        if row.kind == "bind":
            return "n/a"
        return sizes.get(row.name, "") or "-"

    cols = [
        Col("NAME / SOURCE", lambda r: r.name, max=48, min=10, flex=True, want=18),
        Col("KIND", lambda r: r.kind, max=6),
        Col("TYPE", lambda r: r.vtype, max=9, drop=3),
        Col("PROJECT", lambda r: ", ".join(r.projects) or "-", max=18, min=6, flex=True, drop=2),
        Col("USED BY", users, max=30, min=6, flex=True, style=lambda r: () if r.users else ("dim",), want=14),
        Col("SIZE", size, max=8, drop=1),
    ]
    sorts = [("name", lambda xs: sorted(xs, key=lambda r: (r.kind, r.name))),
             ("unused first", lambda xs: sorted(xs, key=lambda r: (bool(r.users), r.kind, r.name))),
             ("project", lambda xs: sorted(xs, key=lambda r: (not r.projects, r.projects, r.name)))]
    ListView("Volumes & bind mounts", fetch, cols, sorts=sorts,
             search=lambda r: " ".join([r.name, r.kind, r.vtype, users(r), r.path] + r.projects),
             on_select=lambda r: mount_view(r.kind, r.name), help_text=VOLUMES_HELP,
             empty="No volumes or bind mounts.", keys={"z": ("sizes", compute_sizes)}).run()


def mount_view(kind: str, name: str) -> None:
    message = None
    while True:
        vols, _ = list_volumes() if kind == "volume" else ([], None)
        cts, _ = list_containers()
        rows = build_mount_rows(vols, cts)
        row = next((r for r in rows if r.kind == kind and r.name == name), None)
        if row is None:
            if kind == "volume":
                print(c("Volume %s no longer exists." % name, "yellow"))
            else:
                print(c("No container uses the bind mount %s any more." % name, "yellow"))
            pause()
            return
        header = [c(("Volume " if kind == "volume" else "Bind mount ") + name, "bold", "cyan")]
        if row.volume:
            v = row.volume
            header += ["Type:        %s Docker volume" % row.vtype,
                       "Mountpoint:  %s" % (v.mountpoint or "-"),
                       "Driver:      %s" % v.driver,
                       "Created:     %s ago" % ago(v.created)]
        else:
            exists = os.path.exists(name)
            header += ["Host path:   %s%s" % (name, "" if exists else c("  (not visible from here)", "yellow"))]
        header.append("Project:     %s" % (", ".join(row.projects) or "-"))
        header.append("Used by:")
        if not row.users:
            header.append("  no containers (running or stopped)")
        for u in row.users:
            header.append("  %s [%s] -> %s%s" % (u.container.name, u.container.state, u.destination,
                                                  "" if u.rw else " (ro)"))
        opts = [("1", "Open a container that uses it", "")] if row.users else []
        if row.volume:
            opts += [("2", "Raw inspect (JSON)", "")]
            if not row.users:
                opts.append(("3", "Remove volume (deletes its data)", "red"))
        key = menu(header, opts, message)
        message = None
        if key == "b":
            return
        if key == "1" and row.users:
            names = [u.container for u in row.users]
            if len(names) == 1:
                container_view(names[0].id)
            else:
                pick = ask("Which? " + "  ".join("%d) %s" % (i, x.name) for i, x in enumerate(names, 1)) + "  : ")
                if pick.isdecimal() and 1 <= int(pick) <= len(names):
                    container_view(names[int(pick) - 1].id)
        elif key == "2" and row.volume:
            r = docker("volume", "inspect", name)
            show_text(r.out if r.ok else r.message(), "Inspect volume " + name)
        elif key == "3" and row.volume and not row.users:
            if remove_volume(row):
                return


def remove_volume(row: MountRow) -> bool:
    clear()
    title_line("Remove volume")
    print("Volume:      %s" % row.name)
    print("Mountpoint:  %s" % (row.path or "-"))
    print("Project:     %s" % (", ".join(row.projects) or "-"))
    print(c("\nThis permanently deletes the data in the volume. Unused does not mean unneeded: "
            "a stopped-and-removed Compose project may expect this data on its next start.", "red"))
    if not typed_confirm(row.name, "delete this volume"):
        print("Cancelled.")
        pause()
        return False
    vols, _ = list_volumes()
    cts, _ = list_containers()
    fresh = next((r for r in build_mount_rows(vols, cts) if r.kind == "volume" and r.name == row.name), None)
    if fresh is None:
        print(c("It is already gone.", "yellow"))
        pause()
        return True
    if fresh.users:
        print(c("A container now uses it (%s). Nothing was removed." % ", ".join(u.container.name for u in fresh.users), "yellow"))
        pause()
        return False
    r = docker("volume", "rm", row.name, timeout=120)
    print(c("Removed volume %s." % row.name, "green") if r.ok else c("Failed: " + r.message(), "red"))
    pause()
    return r.ok


# --------------------------------------------------------------------------- #
# Screens: Compose projects
# --------------------------------------------------------------------------- #

def project_style(p: Project) -> Sequence[str]:
    if p.containers and not p.files:
        return ("red",)
    return {"running": ("green",), "partial": ("yellow",), "stopped": ("yellow",),
            "pick files": ("yellow",)}.get(p.status, ("dim",))


def projects_screen() -> None:
    state: Dict[str, List[str]] = {"notes": []}

    def fetch() -> Tuple[List[Project], Optional[str]]:
        projects, err, notes = load_projects()
        state["notes"] = notes
        return projects, err

    def notes() -> List[str]:
        out = list(CFG.warnings)
        if not CFG.roots:
            out.append("No project folders configured; only deployed projects are listed. "
                       "See h for how to add folders.")
        else:
            out.append("Folders: %s (depth %d)" % (", ".join(CFG.roots), CFG.depth))
        return out + state["notes"]

    def where(p: Project) -> str:
        return p.directory or "-"

    cols = [
        Col("PROJECT", lambda p: p.name, max=28, min=8, flex=True, want=14),
        Col("STATUS", lambda p: p.status_text, max=20, min=7, flex=True, style=project_style, want=12),
        Col("UP", lambda p: "%d/%d" % (p.running, len(p.containers)) if p.containers else "-", max=6),
        Col("FOLDER", where, max=60, min=8, flex=True, drop=1, want=14),
    ]
    order = {"running": 0, "partial": 1, "stopped": 2, "pick files": 3, "not deployed": 4}
    sorts = [("name", lambda xs: sorted(xs, key=lambda p: p.name)),
             ("status", lambda xs: sorted(xs, key=lambda p: (order.get(p.status, 9), p.name))),
             ("folder", lambda xs: sorted(xs, key=lambda p: (p.directory or "~", p.name)))]
    ListView("Compose projects", fetch, cols, sorts=sorts,
             search=lambda p: " ".join([p.name, p.status, p.directory or ""] + list(p.services())),
             on_select=lambda p: project_view(p.key), help_text=PROJECTS_HELP,
             empty="No Compose projects found.", notes=notes).run()


def project_header(p: Project) -> List[str]:
    lines = [c(p.name, "bold", "cyan") + "  " + c(p.status_text, *project_style(p))
             + (c("  (name guessed from folder; Compose decides on first up)", "dim") if not p.name_known else "")]
    lines.append("Folder:   %s" % (p.directory or "-") + ("" if p.directory_ok or not p.directory else c("  (missing)", "red")))
    if p.files:
        how = {"deployed": "as last deployed", "default": "Compose default", "chosen": "chosen by you"}.get(p.files_how, "")
        lines.append("Files:    %s  %s" % (", ".join(_rel(f, p.directory) for f in p.files), c("(" + how + ")", "dim")))
    elif p.containers:
        lines.append("Files:    " + c("not available - Up, Pull and config views are disabled", "red"))
    else:
        lines.append("Files:    " + c("none selected - use Choose compose files", "yellow"))
    unused = [f for f in p.folder_files if os.path.join(p.directory or "", f) not in (p.files or [])]
    if unused:
        lines.append(c("          also here: " + ", ".join(unused), "dim"))
    for problem in p.problems:
        lines.append(c("Note:     " + problem, "yellow"))
    services = p.services()
    if services:
        lines.append("Services:")
        for svc, cts in services.items():
            for ct in cts:
                lines.append("  %-14s %s  %s" % (svc, c(ct.state_text(), *state_style(ct)),
                                                 c(", ".join(ct.ports), "dim")))
    return lines


def _rel(path: str, base: Optional[str]) -> str:
    if base and os.path.dirname(path) == base.rstrip("/"):
        return os.path.basename(path)
    return path


def project_view(key: str) -> None:
    message = None
    directory: Optional[str] = None
    while True:
        p = find_project(key, directory)
        if p is not None:
            key, directory = p.key, p.directory
        if p is None:
            print(c("That project is no longer found (its containers were removed and its folder "
                    "is not under a configured project folder).", "yellow"))
            pause()
            return
        has_ct = bool(p.containers)
        opts: List[Tuple[str, str, str]] = []
        if has_ct or p.files:
            opts.append(("1", "Status (compose ps)", ""))
        if p.can_up:
            opts += [("2", "Up", ""), ("3", "Up with build", ""), ("4", "Pull & up (update images)", "")]
        if has_ct:
            opts += [("5", "Restart", ""), ("6", "Stop", ""), ("7", "Start", ""), ("8", "Logs", "")]
            opts.append(("9", "Down (keep volumes)", "yellow"))
            if p.can_up:
                opts.append(("10", "Down + REMOVE VOLUMES", "red"))
            opts.append(("11", "Containers", ""))
        if p.files:
            opts += [("12", "View compose file(s)", ""), ("13", "Resolved config", ""), ("14", "Environment", "")]
        if len(p.folder_files) > 1 or (not p.files and p.folder_files):
            opts.append(("15", "Choose compose files", ""))
        if len(p.services()) > 1:
            opts.append(("16", "One service...", ""))
        key_in = menu(project_header(p), opts, message)
        message = None
        if key_in == "b":
            return
        if key_in == "" or key_in not in {k for k, _, _ in opts}:
            if key_in:
                message = c("Not available here: %s" % key_in, "yellow")
            continue
        base, cwd = compose_base(p), compose_cwd(p)
        if key_in == "1":
            show_text(run(base + ["ps", "-a"], cwd=cwd).out or "(no containers)", "Status: " + p.name)
        elif key_in == "2":
            compose_run(p, [["up", "-d"]], "Creates or recreates containers so they match the compose file.")
        elif key_in == "3":
            compose_run(p, [["up", "-d", "--build"]],
                        "Builds images that have a build: section, then creates or recreates containers.")
        elif key_in == "4":
            compose_run(p, [["pull"], ["up", "-d"]],
                        "Pulls newer images, then recreates containers whose image changed.")
        elif key_in == "5":
            compose_run(p, [["restart"]], "Restarts the project's containers.")
        elif key_in == "6":
            compose_run(p, [["stop"]], "Stops the containers but keeps them.")
        elif key_in == "7":
            compose_run(p, [["start"]], "Starts the existing containers.", ask_first=False)
        elif key_in == "8":
            project_logs(p)
        elif key_in == "9":
            compose_run(p, [["down"]], "Stops and REMOVES the project's containers and its default network. "
                                       "Named volumes are kept, so data survives the next Up.", default_yes=False)
        elif key_in == "10":
            down_with_volumes(p)
        elif key_in == "11":
            containers_screen(project=p.name)
        elif key_in == "12":
            show_compose_files(p)
        elif key_in == "13":
            print(c("The resolved config fills in values from .env and the environment, so it can "
                    "show passwords and tokens.", "yellow"))
            if confirm("Show it?"):
                page_command(base + ["config"], cwd=cwd, title="Resolved config: " + p.name)
        elif key_in == "14":
            project_env(p)
        elif key_in == "15":
            choose_files(p)
        elif key_in == "16":
            pick_service(p)


def compose_run(p: Project, steps: List[List[str]], explain: str, ask_first: bool = True,
                default_yes: bool = True) -> None:
    base, cwd = compose_base(p), compose_cwd(p)
    clear()
    title_line("%s: %s" % (p.name, " + ".join(" ".join(s) for s in steps)))
    print(explain)
    print(c("\nFolder:  %s" % (cwd or "-"), "dim"))
    for s in steps:
        print(c("Command: " + shlex.join(base + s), "dim"))
    if ask_first and not confirm("\nRun on project %s?" % p.name, default=default_yes):
        return
    print()
    for i, s in enumerate(steps):
        rc = run_fg(base + s, cwd=cwd)
        if rc != 0 and i < len(steps) - 1:
            report_rc(rc)
            if not confirm("'%s' did not finish cleanly. Continue with '%s' anyway?" %
                           (" ".join(s), " ".join(steps[i + 1])), default=False):
                pause()
                return
            continue
        if i == len(steps) - 1:
            print()
            report_rc(rc)
    pause()


def project_logs(p: Project) -> None:
    services = sorted(p.services())
    choice: List[str] = []
    if len(services) > 1:
        clear()
        title_line("Logs: " + p.name)
        print("  0  all services")
        for i, s in enumerate(services, 1):
            print("  %d  %s" % (i, s))
        pick = ask("\nWhich? [0] ")
        if pick == "b":
            return
        if pick.isdecimal() and 1 <= int(pick) <= len(services):
            choice = [services[int(pick) - 1]]
    base, cwd = compose_base(p), compose_cwd(p)
    title = "Logs: %s%s" % (p.name, (" / " + choice[0]) if choice else " (all services)")
    logs_menu(title, lambda opts: base + ["logs"] + opts + choice, cwd=cwd)


def service_names(p: Project) -> Tuple[List[str], Optional[str]]:
    """Services from the compose file(s), plus any that only exist as containers."""
    names = set(p.services()) if p.containers else set()
    err = None
    if p.files:
        r = run(compose_base(p) + ["config", "--services"], cwd=compose_cwd(p), timeout=60)
        if r.ok:
            names.update(r.out.split())
        else:
            err = r.message()
    return sorted(names), err


def pick_service(p: Project) -> None:
    clear()
    title_line("Services of " + p.name)
    print("Reading the compose file...")
    names, err = service_names(p)
    if err:
        print(c("Could not read the compose file: " + err.splitlines()[0], "yellow"))
    if not names:
        print("No services found.")
        pause()
        return
    if len(names) == 1:
        service_view(p.key, p.directory, names[0])
        return
    states = p.services()
    for i, n in enumerate(names, 1):
        cts = states.get(n, [])
        print("  %d  %-20s %s" % (i, n, ", ".join(ct.state_text() for ct in cts) or c("no container", "dim")))
    pick = ask("\nWhich service? ")
    if pick.isdecimal() and 1 <= int(pick) <= len(names):
        service_view(p.key, p.directory, names[int(pick) - 1])


def service_view(key: str, directory: Optional[str], svc: str) -> None:
    message = None
    while True:
        p = find_project(key, directory)
        if p is None:
            print(c("That project is no longer found.", "yellow"))
            pause()
            return
        key, directory = p.key, p.directory
        cts = p.services().get(svc, [])
        header = [c("%s / %s" % (p.name, svc), "bold", "cyan")]
        for ct in cts:
            header.append("  %s  %s  %s" % (ct.name, c(ct.state_text(), *state_style(ct)), c(", ".join(ct.ports), "dim")))
        if not cts:
            header.append(c("  no container yet", "dim"))
        opts: List[Tuple[str, str, str]] = []
        if cts:
            opts += [("1", "Logs", ""), ("2", "Restart", ""), ("3", "Stop", ""), ("4", "Start", "")]
        if p.can_up:
            opts += [("5", "Up (create or recreate)", ""), ("6", "Up with build", ""), ("7", "Pull & up", "")]
        if cts:
            opts.append(("8", "Container" + ("s" if len(cts) > 1 else ""), ""))
        key_in = menu(header, opts, message)
        message = None
        if key_in == "b":
            return
        if key_in == "" or key_in not in {k for k, _, _ in opts}:
            if key_in:
                message = c("Not available here: %s" % key_in, "yellow")
            continue
        deps = "Services it depends on are started too if they are not running."
        if key_in == "1":
            logs_menu("Logs: %s / %s" % (p.name, svc), lambda opts, p=p: compose_base(p) + ["logs"] + opts + [svc],
                      cwd=compose_cwd(p))
        elif key_in == "2":
            compose_run(p, [["restart", svc]], "Restarts the %s container(s)." % svc)
        elif key_in == "3":
            compose_run(p, [["stop", svc]], "Stops the %s container(s) but keeps them." % svc)
        elif key_in == "4":
            compose_run(p, [["start", svc]], "Starts the existing %s container(s)." % svc, ask_first=False)
        elif key_in == "5":
            compose_run(p, [["up", "-d", svc]], "Creates or recreates %s so it matches the compose file. %s" % (svc, deps))
        elif key_in == "6":
            compose_run(p, [["up", "-d", "--build", svc]],
                        "Builds %s if it has a build: section, then creates or recreates it. %s" % (svc, deps))
        elif key_in == "7":
            compose_run(p, [["pull", svc], ["up", "-d", svc]],
                        "Pulls a newer image for %s, then recreates it if the image changed. %s" % (svc, deps))
        elif key_in == "8":
            if len(cts) == 1:
                container_view(cts[0].id)
            else:
                pick = ask("Which? " + "  ".join("%d) %s" % (i, x.name) for i, x in enumerate(cts, 1)) + "  : ")
                if pick.isdecimal() and 1 <= int(pick) <= len(cts):
                    container_view(cts[int(pick) - 1].id)


@dataclass
class VolumeImpact:
    remove: List[str]             # existing volumes that `down -v` would delete
    absent: List[str]             # declared but not present
    external: List[str]           # declared external; kept
    anonymous: List[str]          # anonymous volumes attached to project containers
    certain: bool
    notes: List[str]

    def snapshot(self) -> Tuple[Any, ...]:
        return tuple(sorted(self.remove)), tuple(sorted(self.anonymous)), self.certain


def volume_impact(p: Project, cfg: Optional[Dict[str, Any]], cfg_err: Optional[str],
                  existing: Sequence[str]) -> VolumeImpact:
    declared, external, notes = [], [], []
    certain = cfg is not None
    if cfg is None:
        notes.append("Could not read the compose config (%s). Compose will still remove every "
                     "non-external volume the file declares." % (cfg_err or "unknown error"))
    else:
        for key, spec in (cfg.get("volumes") or {}).items():
            spec = spec or {}
            name = spec.get("name") or "%s_%s" % (p.name, key)
            (external if spec.get("external") else declared).append(name)
    anonymous = sorted({m.get("Name", "") for ct in p.containers for m in ct.mounts
                        if m.get("Type") == "volume" and re.fullmatch(r"[0-9a-f]{64}", m.get("Name", ""))})
    exist = set(existing)
    return VolumeImpact(remove=sorted(n for n in declared if n in exist),
                        absent=sorted(n for n in declared if n not in exist),
                        external=sorted(external), anonymous=anonymous, certain=certain, notes=notes)


def current_impact(p: Project) -> Tuple[VolumeImpact, Tuple[Any, ...]]:
    cfg, err = compose_config_json(p)
    r = docker("volume", "ls", "-q")
    impact = volume_impact(p, cfg, err, r.out.split() if r.ok else [])
    snap = (p.name, tuple(p.files or []), tuple(sorted(ct.id for ct in p.containers))) + impact.snapshot()
    return impact, snap


def down_with_volumes(p: Project) -> None:
    impact, snap = current_impact(p)
    clear()
    print(c("DOWN + REMOVE VOLUMES: %s" % p.name, "bold", "red"))
    print("Stops and removes the project's containers and network, AND deletes its volumes.")
    print(c("Data in these volumes (databases, uploads, app state) is gone for good. "
            "Running Up again creates empty volumes; it does not bring the data back.", "red"))
    print()
    print("Volumes that will be deleted:")
    for n in impact.remove:
        print("  " + c(n, "bold"))
    for n in impact.anonymous:
        print("  %s %s" % (n[:12], c("(anonymous, attached to a project container)", "dim")))
    if not impact.remove and not impact.anonymous:
        print("  (none found)" if impact.certain else "  (unknown)")
    if impact.absent:
        print(c("Declared but not present: " + ", ".join(impact.absent), "dim"))
    if impact.external:
        print(c("External volumes are kept: " + ", ".join(impact.external), "dim"))
    for n in impact.notes:
        print(c(n, "yellow"))
    print(c("\nCommand: " + shlex.join(compose_base(p) + ["down", "-v"]), "dim"))
    print()
    if not typed_confirm(p.name, "delete these volumes"):
        print("Cancelled. Nothing was changed.")
        pause()
        return
    fresh = find_project(p.key)
    if fresh is None or fresh.key != p.key:
        print(c("The project changed (it is no longer found). Nothing was run.", "yellow"))
        pause()
        return
    _, fresh_snap = current_impact(fresh)
    if fresh_snap != snap:
        print(c("The project or its volumes changed since you reviewed them. Nothing was run; "
                "open this action again to review the new state.", "yellow"))
        pause()
        return
    print()
    rc = run_fg(compose_base(fresh) + ["down", "-v"], cwd=compose_cwd(fresh))
    print()
    report_rc(rc)
    pause()


def show_compose_files(p: Project) -> None:
    parts = []
    for f in p.files or []:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                parts.append("# ---- %s ----\n%s" % (f, fh.read()))
        except OSError as e:
            parts.append("# ---- %s ----\n(cannot read: %s)" % (f, e.strerror or e))
    show_text("\n\n".join(parts) or "(no files)", "Compose files: " + p.name)


def project_env(p: Project) -> None:
    sections: List[Tuple[str, List[Tuple[str, str]]]] = []
    note = (".env in the project folder fills in ${VARIABLES} in the compose file. Its values are "
            "not passed into containers unless a service lists them under environment: or env_file:.")
    if p.directory:
        dotenv = os.path.join(p.directory, ".env")
        pairs, err = read_dotenv(dotenv)
        if err:
            sections.append((dotenv + " (" + err + ")", []))
        elif pairs is None:
            sections.append((dotenv + " (not present)", []))
        else:
            sections.append((dotenv, pairs))
    print("Reading compose config...")
    cfg, err = compose_config_json(p)
    if cfg is None:
        sections.append(("Service environment (unavailable: %s)" % err, []))
    else:
        for svc, spec in sorted((cfg.get("services") or {}).items()):
            env = (spec or {}).get("environment") or {}
            if isinstance(env, list):
                pairs = split_env(env)
            else:
                pairs = [(k, "" if v is None else str(v)) for k, v in sorted(env.items())]
            sections.append(("Service %s (environment + env_file, resolved)" % svc, pairs))
    show_env("Environment: " + p.name, sections, note)


def choose_files(p: Project) -> None:
    clear()
    title_line("Choose compose files for " + p.name)
    names = p.folder_files
    for i, n in enumerate(names, 1):
        print("  %d  %s" % (i, n))
    print(c("\nEnter numbers in the order Compose should apply them (later files override "
            "earlier ones), e.g. '1 3'. 'd' restores the default. Applies until dman exits.", "dim"))
    pick = ask("> ", on_interrupt="")
    if pick.lower() == "d":
        CHOSEN_FILES.pop(p.key, None)
        return
    idx: List[int] = []
    for part in re.split(r"[,\s]+", pick.strip()):
        if part.isdecimal() and 1 <= int(part) <= len(names) and int(part) - 1 not in idx:
            idx.append(int(part) - 1)
        elif part:
            print(c("Invalid choice.", "yellow"))
            pause()
            return
    if idx:
        CHOSEN_FILES[p.key] = [os.path.join(p.directory or "", names[i]) for i in idx]


# --------------------------------------------------------------------------- #
# Screens: disk usage and cleanup
# --------------------------------------------------------------------------- #

def cleanup_screen() -> None:
    message = None
    while True:
        r = docker("system", "df", "--format", "{{json .}}", timeout=120)
        rows = parse_json_lines(r.out)
        header = [c("Disk usage & cleanup", "bold", "cyan")]
        if rows:
            header.append(c("%-15s %6s %7s %10s  %s" % ("TYPE", "TOTAL", "ACTIVE", "SIZE", "RECLAIMABLE (estimate)"), "dim"))
            for row in rows:
                header.append("%-15s %6s %7s %10s  %s" % (row.get("Type", "?"), row.get("TotalCount", "-"),
                                                           row.get("Active", "-"), row.get("Size", "-"),
                                                           row.get("Reclaimable", "-")))
        else:
            header.append(c("Docker did not report disk usage: " + r.message(), "yellow"))
        header.append(c("Volumes are never removed here. Remove a volume on purpose from Volumes.", "dim"))
        key = menu(header, [("1", "Remove stopped containers...", ""), ("2", "Remove unused images...", ""),
                            ("3", "Clear build cache...", ""), ("4", "Detailed usage (docker system df -v)", "")],
                   message)
        message = None
        if key == "b":
            return
        if key == "1":
            cleanup_containers()
        elif key == "2":
            cleanup_images()
        elif key == "3":
            cleanup_build_cache()
        elif key == "4":
            print("Asking Docker (this can take a while)...")
            page_command(["docker", "system", "df", "-v"], title="docker system df -v")


def pick_candidates(title: str, lines: List[str], explain: str) -> Optional[List[int]]:
    clear()
    title_line(title)
    print(c(explain, "dim"))
    print()
    if not lines:
        print("  Nothing to clean up.")
        pause()
        return None
    width = len(str(len(lines)))
    cols, _ = term_size()
    for i, line in enumerate(lines, 1):
        print(fit("%s  %s" % (str(i).rjust(width), line), cols - 1))
    print()
    raw = ask("Select (e.g. 1 3-5, 'all'; Enter cancels): ", on_interrupt="")
    if not raw:
        return None
    picked = parse_selection(raw, len(lines))
    if picked is None:
        print(c("That selection is not valid.", "yellow"))
        pause()
    return picked


def cleanup_containers() -> None:
    cts = sorted([ct for ct in list_containers()[0] if ct.stopped], key=lambda x: x.name)
    lines = ["%-28s %s  %-12s %s" % (fit(ct.name, 28), ct.short_id, ct.state_text(),
                                     ("%s/%s" % (ct.project, ct.service)) if ct.project else "")
             for ct in cts]
    picked = pick_candidates("Stopped containers", lines,
                             "Exited, created and dead containers. Removing a stopped Compose container is "
                             "usually safe; Compose recreates it on the next Up. Volumes are kept.")
    if not picked:
        return
    chosen = [cts[i] for i in picked]
    print()
    for ct in chosen:
        print("  " + ct.label())
    if not confirm("Remove these %d container(s)?" % len(chosen)):
        return
    current = {ct.id: ct for ct in list_containers()[0]}
    changed = [ct for ct in chosen if ct.id not in current or not current[ct.id].stopped]
    if changed:
        print(c("These changed since you reviewed them (started or removed): " +
                ", ".join(ct.name for ct in changed) + ". Nothing was removed; please review again.", "yellow"))
        pause()
        return
    r = docker("rm", *[ct.id for ct in chosen], timeout=300)
    print(c("Removed %d container(s)." % len(chosen), "green") if r.ok else c("Problem: " + r.message(), "red"))
    pause()


def cleanup_images() -> None:
    images, _ = list_images()
    containers, _ = list_containers()
    unused = sorted([i for i in images if not image_users(i, containers)], key=lambda i: -(i.size or 0))
    lines = ["%-40s %s %9s  %s" % (fit(i.name, 40), i.short_id, human_bytes(i.size),
                                   ("built " + ago(i.created) + " ago") + ("  [untagged]" if not i.tags else ""))
             for i in unused]
    picked = pick_candidates("Unused images", lines,
                             "Images no container (running or stopped) uses. A Compose project you start "
                             "later may still need one; it would be pulled or built again. Sizes include "
                             "shared layers, so space freed can be less.")
    if not picked:
        return
    remove_images([unused[i] for i in picked])


def cleanup_build_cache() -> None:
    rows = parse_json_lines(docker("system", "df", "--format", "{{json .}}", timeout=120).out)
    bc = next((r for r in rows if r.get("Type") == "Build Cache"), {})
    key = menu([c("Build cache", "bold", "cyan"),
                "Size: %s   Reclaimable: %s" % (bc.get("Size", "-"), bc.get("Reclaimable", "-")),
                c("Build cache only speeds up later builds; clearing it never touches containers, "
                  "images in use or volumes.", "dim")],
               [("1", "Remove unused build cache  (docker builder prune)", ""),
                ("2", "Remove ALL build cache  (docker builder prune -a)", "")])
    if key not in ("1", "2"):
        return
    args = ["docker", "builder", "prune", "-f"] + (["-a"] if key == "2" else [])
    if confirm("Run `%s`?" % shlex.join(args)):
        rc = run_fg(args)
        report_rc(rc)
        pause()


# --------------------------------------------------------------------------- #
# Screens: system info and help
# --------------------------------------------------------------------------- #

ENDPOINT: Optional[Endpoint] = None


def system_screen() -> None:
    print("Collecting...")
    lines = []
    ver = docker("version", "--format", "{{json .}}", timeout=30)
    try:
        v = json.loads(ver.out) if ver.out.strip() else {}
    except ValueError:
        v = {}
    client, server = v.get("Client") or {}, v.get("Server") or {}
    compose = docker("compose", "version", "--short", timeout=30)
    lines += ["dman:            %s (Python %s)" % (read_version() or "unknown version", sys.version.split()[0]),
              "Docker CLI:      %s" % client.get("Version", "?"),
              "Docker Engine:   %s (API %s)" % (server.get("Version", "?"), server.get("ApiVersion", "?")),
              "Compose:         %s" % (compose.out.strip() if compose.ok else "not available"),
              ""]
    if ENDPOINT:
        lines += ["Context:         %s" % ENDPOINT.context,
                  "Endpoint:        %s" % ENDPOINT.host,
                  "Local check:     %s" % ("OK - local Unix socket" if not ENDPOINT.problem else ENDPOINT.problem),
                  ""]
    info = docker("info", "--format", "{{json .}}", timeout=30)
    try:
        d = json.loads(info.out) if info.out.strip() else {}
    except ValueError:
        d = {}
    if d:
        lines += ["Host:            %s" % d.get("Name", "?"),
                  "OS / kernel:     %s / %s" % (d.get("OperatingSystem", "?"), d.get("KernelVersion", "?")),
                  "CPUs / memory:   %s / %s" % (d.get("NCPU", "?"), human_bytes(d.get("MemTotal"))),
                  "Docker root:     %s" % d.get("DockerRootDir", "?"),
                  "Storage driver:  %s" % d.get("Driver", "?"),
                  "Logging driver:  %s" % d.get("LoggingDriver", "?"),
                  "Cgroup driver:   %s (v%s)" % (d.get("CgroupDriver", "?"), d.get("CgroupVersion", "?")),
                  "Containers:      %s (%s running, %s paused, %s stopped)" % (
                      d.get("Containers", "?"), d.get("ContainersRunning", "?"),
                      d.get("ContainersPaused", "?"), d.get("ContainersStopped", "?")),
                  "Images:          %s" % d.get("Images", "?")]
        for w in d.get("Warnings") or []:
            lines.append("Warning:         %s" % w)
    else:
        lines.append("docker info failed: " + info.message())
    lines += ["", "Config file:     %s" % (CFG.path or "none found (looked in %s and %s)" % (USER_CONFIG, SYSTEM_CONFIG)),
              "Project folders: %s" % ("none" if not CFG.roots else ""),
              ]
    for root in CFG.roots:
        lines.append("  %s%s" % (root, "" if os.path.isdir(root) else "  (missing)"))
    lines.append("Search depth:    %d" % CFG.depth)
    for w in CFG.warnings:
        lines.append("Config warning:  %s" % w)
    show_text("\n".join(lines), "System info")


LIST_HELP = """Lists
  1-N       open an item on this page
  n / p     next / previous page
  s         choose how to sort        f   filter (words; all must match)
  Enter     refresh                   b   back      q   quit dman
Ctrl-C at a prompt goes back. Item numbers are only valid for the page shown;
dman re-checks the real container/image/volume before acting on it."""

CONTAINERS_HELP = LIST_HELP + """

Containers
  Lists every container, running or stopped. STATE shows the exit code for
  stopped containers and the health status when the image defines a check.
  PORTS shows published ports as host->container (an IP prefix means the port
  is only published on that address).
  STATE also shows how often the restart policy has restarted a container since
  it was last started by hand; a running container with restarts is yellow,
  which usually means it keeps crashing.
  Open a container for logs, start/stop/restart, a shell, one-off commands,
  details, environment (values hidden by default), mounts and removal.
  Logs: last N lines, follow live, or everything since a time (15m, 2h,
  2026-10-22); t toggles timestamps."""

STATS_HELP = LIST_HELP + """

Live resource usage
  Current CPU, memory, network and disk I/O for running containers, as reported
  by `docker stats`. Nothing is recorded. Press a to refresh every few seconds
  (Ctrl-C stops). '-' means Docker had no value, which is not the same as 0."""

IMAGES_HELP = LIST_HELP + """

Images
  CONTAINERS counts containers using the image, stopped ones included. An unused
  image may still be needed by a Compose project's next deploy.
  SIZE includes layers shared with other images, so removing an image can free
  less than its size. CREATED is the build date, not the download date."""

VOLUMES_HELP = LIST_HELP + """

Volumes & bind mounts
  KIND volume = Docker-managed volume; bind = host folder mounted into a container
  (bind mounts are listed only while some container uses them).
  TYPE named / anonymous; 'unknown' when Docker gives no reliable signal.
  z asks Docker for volume sizes (it scans the data, so it is on demand).
  Removing a volume deletes its data. dman only offers it for volumes no
  container uses, asks you to type the name, and re-checks right before."""

PROJECTS_HELP = LIST_HELP + """

Compose projects
  Projects come from two places:
   - folders listed under [projects] roots in the config file, searched to the
     configured depth (default 2) for compose.yaml / docker-compose.yml and
     variants such as docker-compose.prod.yml;
   - containers that Compose created (their labels), wherever they came from.
  A folder project that has never been started shows as 'not deployed' and can
  be started with Up. 'pick files' means a folder has several variant files and
  no standard one: open it and choose which to use.

  One service      shown for projects with several services: logs, restart,
                   stop, start, up, up with build and pull & up for just one
                   (Up also starts the services it depends on)

  Up               docker compose up -d
  Up with build    docker compose up -d --build
  Pull & up        docker compose pull, then up -d (how you update images)
  Down             removes containers + network, KEEPS volumes
  Down + remove    docker compose down -v - deletes the project's volumes and
  volumes          their data. Needs the project name typed, and is cancelled if
                   anything changed between your review and the run.

  Config file: %s
  Example:
    [projects]
    roots =
        /opt/docker
        /srv/stacks
    depth = 2""" % (SYSTEM_CONFIG,)

MAIN_HELP = """dman is a terminal manager for the Docker host it runs on.

Main menu
  1 Compose projects   find, start, update, stop and tear down Compose projects
  2 Containers         every container; logs, shell, details, start/stop
  3 Live usage         CPU / memory / I/O right now
  4 Images             sizes, what uses them, pull, remove
  5 Volumes            volumes and bind mounts, what uses them, remove
  6 Disk & cleanup     docker system df and reviewed cleanup (never volumes)
  7 System info        versions, endpoint, config in use

Everywhere: Enter refreshes, b goes back, q quits, h shows help, Ctrl-C at a
prompt goes back, and Ctrl-C stops a running command (logs, up, ...).

Configuration
  First found of: --config FILE, $DMAN_CONFIG, ~/.config/dman/dman.conf,
  /etc/dman/dman.conf.

    [projects]
    roots =
        /opt/docker
    depth = 2

    [ui]
    page_size = auto

Safety
  dman refuses to run against a remote or ambiguous Docker endpoint. Destructive
  actions show what they affect, ask first, and re-check the targets right
  before running. Volumes are only removed by explicit volume actions."""


# --------------------------------------------------------------------------- #
# Startup and main menu
# --------------------------------------------------------------------------- #

def preflight() -> Optional[str]:
    """Return an error message if dman cannot run, after pinning DOCKER_HOST to the
    verified local socket for every later Docker and Compose call."""
    global ENDPOINT
    if not shutil.which("docker"):
        return "The docker command was not found. Install Docker Engine and its CLI first."
    ep = resolve_endpoint()
    ENDPOINT = ep
    if ep.problem:
        return ep.problem
    os.environ["DOCKER_HOST"] = ep.host
    os.environ.pop("DOCKER_CONTEXT", None)
    r = docker("version", "--format", "{{.Server.Version}}", timeout=20)
    if not r.ok or not r.out.strip():
        return "Cannot talk to the Docker daemon at %s.\n%s" % (ep.host, r.message())
    if not docker("compose", "version", "--short", timeout=20).ok:
        return ("The Docker Compose plugin is not available (`docker compose`). On Debian install "
                "the docker-compose-plugin package.")
    return None


def main_menu() -> None:
    screens: Dict[str, Callable[[], None]] = {
        "1": projects_screen, "2": containers_screen, "3": stats_screen, "4": images_screen,
        "5": volumes_screen, "6": cleanup_screen, "7": system_screen,
    }
    message = None
    while True:
        r = docker("ps", "-a", "--format", "{{.State}}", timeout=20)
        states = r.out.split() if r.ok else []
        summary = ("%d containers, %d running" % (len(states), states.count("running"))) if r.ok else "Docker not responding"
        key = menu([c("dman", "bold", "cyan") + c("  Docker on this host - " + summary, "dim")],
                   [("1", "Compose projects", ""), ("2", "Containers", ""), ("3", "Live resource usage", ""),
                    ("4", "Images", ""), ("5", "Volumes & mounts", ""), ("6", "Disk usage & cleanup", ""),
                    ("7", "System info", ""), ("h", "Help", "")], message)
        message = None
        if key in screens:
            try:
                screens[key]()
            except KeyboardInterrupt:
                message = c("Interrupted.", "yellow")
        elif key == "h":
            show_text(MAIN_HELP, "dman help")
        elif key == "b":
            if confirm("Quit dman?", default=True):
                return
        elif key:
            message = c("Unknown choice: %s" % key, "yellow")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog=APP, description="Terminal manager for the local Docker host and its Compose projects.",
        epilog="Run without arguments for the interactive menu. Config: %s or %s." % (USER_CONFIG, SYSTEM_CONFIG))
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--config", metavar="FILE", help="use this config file")
    args = parser.parse_args(argv)

    if args.version:
        version = read_version()
        if version is None:
            print("%s: VERSION file is missing or invalid" % APP, file=sys.stderr)
            return 1
        print("%s %d" % (APP, version))
        return 0

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("%s is interactive; run it in a terminal." % APP, file=sys.stderr)
        return 2

    global CFG
    CFG = load_config(args.config)
    sys.stdout = SafeOutput(sys.stdout)
    init_terminal()
    problem = preflight()
    if problem:
        print("%s: %s" % (APP, strip_controls(problem)), file=sys.stderr)
        return 1
    try:
        main_menu()
    except QuitApp:
        pass
    except KeyboardInterrupt:
        print()
    clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
