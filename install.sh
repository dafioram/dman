#!/usr/bin/env bash
# install.sh - install dman system-wide.
#
#   sudo ./install.sh            install, or replace an existing dman install
#   sudo ./install.sh --yes      same, without questions (keeps or writes a default config)
#
# Installs:
#   /usr/local/lib/dman/   dman.py, VERSION, .dman-install-marker
#   /usr/local/bin/dman    small launcher
#   /etc/dman/dman.conf    only if it does not exist yet (never overwritten)
#
# It never touches Docker, containers, images, volumes or Compose files, and makes no backups.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${DMAN_PREFIX:-/usr/local}"
CONF_DIR="${DMAN_CONF_DIR:-/etc/dman}"
LIB="$PREFIX/lib/dman"
BIN="$PREFIX/bin/dman"
CONF="$CONF_DIR/dman.conf"
MARKER=".dman-install-marker"
LAUNCHER_TAG="# dman-launcher: managed by dman install.sh"

YES=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) YES=1 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install: unknown option: $arg" >&2; exit 2 ;;
    esac
done

die() { echo "install: $*" >&2; exit 1; }
say() { echo "  $*"; }

echo "Installing dman"

# 1. Package files
for f in dman.py VERSION dman.conf.example; do
    [ -f "$SRC/$f" ] && [ -r "$SRC/$f" ] || die "missing or unreadable package file: $SRC/$f"
done

# 2. Version: exactly one positive whole number, optionally followed by a newline
VERSION="$(cat "$SRC/VERSION")"
[[ "$VERSION" =~ ^[1-9][0-9]*$ ]] || die "VERSION must contain one positive whole number (found: '$VERSION')"
if ! { printf '%s\n' "$VERSION" | cmp -s - "$SRC/VERSION" || printf '%s' "$VERSION" | cmp -s - "$SRC/VERSION"; }; then
    die "VERSION must contain only the number and an optional newline"
fi

# 3. Python 3.9+
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 was not found. On Debian: sudo apt install python3"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "dman needs Python 3.9 or newer; $PYTHON is $("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

command -v docker >/dev/null 2>&1 || say "note: the docker command is not installed yet; dman needs it to run."

# 4. Permissions (never elevate silently)
mkdir -p "$PREFIX/lib" "$PREFIX/bin" 2>/dev/null || true
if [ ! -w "$PREFIX/lib" ] || [ ! -w "$PREFIX/bin" ]; then
    die "no write access to $PREFIX/lib and $PREFIX/bin. Run it with sudo:  sudo ./install.sh"
fi

# 5. Only replace what an earlier dman install created
if [ -L "$LIB" ]; then die "$LIB is a symlink; refusing to replace it"; fi
if [ -L "$BIN" ]; then die "$BIN is a symlink; refusing to replace it"; fi
if [ -e "$LIB" ]; then
    [ -d "$LIB" ] && [ -f "$LIB/$MARKER" ] \
        || die "$LIB exists but was not installed by dman (no $MARKER); refusing to touch it"
fi
if [ -e "$BIN" ]; then
    [ -f "$BIN" ] && grep -qF "$LAUNCHER_TAG" "$BIN" \
        || die "$BIN exists but is not the dman launcher; refusing to overwrite it"
fi
OLD=""
if [ -e "$LIB/VERSION" ]; then OLD="$(cat "$LIB/VERSION" 2>/dev/null || true)"; fi

# 6. Stage the new files next to the target, check they run, then swap them in
STAGE="$(mktemp -d "$PREFIX/lib/.dman-stage.XXXXXX")"
LTMP=""
cleanup() { rm -rf "$STAGE"; if [ -n "$LTMP" ]; then rm -f "$LTMP"; fi; }
trap cleanup EXIT
install -m 0755 "$SRC/dman.py" "$STAGE/dman.py"
install -m 0644 "$SRC/VERSION" "$STAGE/VERSION"
printf 'Installed by dman install.sh. Removing this file stops install.sh/uninstall.sh from managing this folder.\n' > "$STAGE/$MARKER"
chmod 0644 "$STAGE/$MARKER"
chmod 0755 "$STAGE"
got="$("$PYTHON" "$STAGE/dman.py" --version 2>&1)" || die "the new dman.py failed to start: $got"
[ "$got" = "dman $VERSION" ] || die "the new dman.py reported '$got', expected 'dman $VERSION'"

LTMP="$(mktemp "$PREFIX/bin/.dman-launcher.XXXXXX")"
cat > "$LTMP" <<EOF
#!/bin/sh
$LAUNCHER_TAG
exec "$PYTHON" "$LIB/dman.py" "\$@"
EOF
chmod 0755 "$LTMP"

# Not atomic: the old folder is removed and the staged one moved in right after.
if [ -d "$LIB" ]; then rm -rf "$LIB"; fi
mv "$STAGE" "$LIB"
mv -f "$LTMP" "$BIN"
LTMP=""

# 7. Config (created once, never overwritten)
if [ -e "$CONF" ]; then
    say "keeping existing config $CONF"
else
    roots="/opt/docker"
    if [ "$YES" = 0 ] && [ -t 0 ]; then
        echo
        echo "Where are your Compose projects? dman searches these folders and two levels below."
        read -r -p "Folders, separated by spaces [$roots]: " answer || answer=""
        if [ -n "$answer" ]; then roots="$answer"; fi
    fi
    mkdir -p "$CONF_DIR"
    {
        sed -n '1,/^roots =/p' "$SRC/dman.conf.example"
        for r in $roots; do printf '    %s\n' "$r"; done
        sed -n '/^roots =/,$p' "$SRC/dman.conf.example" | sed '1,2d'
    } > "$CONF"
    chmod 0644 "$CONF"
    say "wrote config $CONF (project folders: $roots)"
fi

# 8. Verify the installed command
got="$("$BIN" --version 2>&1)" || die "installed, but $BIN --version failed: $got"
[ "$got" = "dman $VERSION" ] || die "installed, but $BIN reports '$got'"

if [ -n "$OLD" ] && [ "$OLD" != "$VERSION" ]; then
    say "replaced version $OLD"
fi
say "installed dman $VERSION at $BIN"
echo "Run: dman   (Docker access usually needs root or membership of the docker group)"
