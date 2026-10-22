#!/usr/bin/env bash
# uninstall.sh - remove the system-wide dman command.
#
#   sudo ./uninstall.sh           asks before removing
#   sudo ./uninstall.sh --yes     no questions; keeps the config file
#   sudo ./uninstall.sh --purge   also removes /etc/dman/dman.conf
#
# Removes only /usr/local/bin/dman and /usr/local/lib/dman, after checking dman installed them.
# Docker, containers, images, volumes, Compose files and this source folder are left alone.

set -euo pipefail

PREFIX="${DMAN_PREFIX:-/usr/local}"
CONF_DIR="${DMAN_CONF_DIR:-/etc/dman}"
LIB="$PREFIX/lib/dman"
BIN="$PREFIX/bin/dman"
CONF="$CONF_DIR/dman.conf"
MARKER=".dman-install-marker"
LAUNCHER_TAG="# dman-launcher: managed by dman install.sh"

YES=0
PURGE=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) YES=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "uninstall: unknown option: $arg" >&2; exit 2 ;;
    esac
done

die() { echo "uninstall: $*" >&2; exit 1; }

remove_lib=0
remove_bin=0
if [ -L "$LIB" ]; then die "$LIB is a symlink, not a dman install; refusing to touch it"; fi
if [ -L "$BIN" ]; then die "$BIN is a symlink, not the dman launcher; refusing to touch it"; fi
if [ -e "$LIB" ]; then
    [ -d "$LIB" ] && [ -f "$LIB/$MARKER" ] || die "$LIB was not installed by dman (no $MARKER); refusing to remove it"
    remove_lib=1
fi
if [ -e "$BIN" ]; then
    [ -f "$BIN" ] && grep -qF "$LAUNCHER_TAG" "$BIN" || die "$BIN is not the dman launcher; refusing to remove it"
    remove_bin=1
fi
if [ "$remove_lib" = 0 ] && [ "$remove_bin" = 0 ]; then
    echo "dman is not installed under $PREFIX. Nothing to do."
    exit 0
fi

parent="$PREFIX/lib"
if [ ! -w "$parent" ] || [ ! -w "$PREFIX/bin" ]; then
    die "no write access to $PREFIX. Run it with sudo:  sudo ./uninstall.sh"
fi

echo "This removes:"
[ "$remove_bin" = 1 ] && echo "  $BIN"
[ "$remove_lib" = 1 ] && echo "  $LIB/"
echo "Docker and everything it manages is left untouched."
if [ "$YES" = 0 ]; then
    read -r -p "Remove dman? [y/N] " answer || answer=""
    case "$answer" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac
fi

[ "$remove_bin" = 1 ] && rm -f "$BIN"
[ "$remove_lib" = 1 ] && rm -rf "$LIB"
echo "Removed dman."

if [ -e "$CONF" ]; then
    drop=$PURGE
    if [ "$drop" = 0 ] && [ "$YES" = 0 ]; then
        read -r -p "Also remove the config file $CONF? [y/N] " answer || answer=""
        case "$answer" in y|Y|yes|YES) drop=1 ;; esac
    fi
    if [ "$drop" = 1 ]; then
        rm -f "$CONF"
        rmdir "$CONF_DIR" 2>/dev/null || true
        echo "Removed $CONF."
    else
        echo "Kept $CONF."
    fi
fi
