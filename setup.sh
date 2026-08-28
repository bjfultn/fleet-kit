#!/bin/bash
# Interactive fleet provisioner. Thin wrapper: it finds a usable interpreter,
# then hands over to bin/fleet-setup.py, which does the real work.
#
#   ./setup.sh                 interactive
#   ./setup.sh --spec f.json   non-interactive, answers from a file
#   ./setup.sh --dry-run       render to stdout, write nothing

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pick_python() {
    for c in "${FLEET_PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        [ -n "$c" ] || continue
        command -v "$c" >/dev/null 2>&1 || continue
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            echo "$c"; return 0
        fi
    done
    return 1
}

PY="$(pick_python)" || {
    echo "setup: no Python 3.10+ found." >&2
    echo "       The mention watcher needs 3.10+. macOS ships 3.9 at /usr/bin/python3;" >&2
    echo "       install a newer one (brew install python) and re-run." >&2
    exit 1
}

exec "$PY" "$HERE/bin/fleet-setup.py" "$@"
