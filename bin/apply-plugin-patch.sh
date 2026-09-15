#!/bin/bash
# Apply the Discord plugin patch, or fail loudly.
#
# Stock discord plugin v0.0.4 drops every bot-authored message before any
# access check runs. Unpatched, agents cannot wake each other and nothing
# anywhere logs an error, so the fleet looks healthy and is not. That failure
# mode is the reason this is a script and not a line in the README: a patch
# that silently did not apply produces exactly the same silence.
#
#   bin/apply-plugin-patch.sh           patch every cached discord plugin
#   bin/apply-plugin-patch.sh --check   report status, change nothing
#
# Exits 0 when every cached copy is patched, 1 otherwise.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$(cd "$HERE/.." && pwd)/patches/discord-allowbots.patch"

# The plugin version this patch was generated against and verified on.
CUT_AGAINST="0.0.4"
CACHE="${FLEET_PLUGIN_CACHE:-$HOME/.claude/plugins/cache/claude-plugins-official/discord}"

check_only=0
case "${1:-}" in
    --check) check_only=1 ;;
    "") ;;
    *) echo "usage: $(basename "$0") [--check]" >&2; exit 1 ;;
esac

[ -f "$PATCH" ] || { echo "patch: missing $PATCH" >&2; exit 1; }

shopt -s nullglob
dirs=("$CACHE"/*/)
if [ ${#dirs[@]} -eq 0 ]; then
    echo "patch: no discord plugin found under $CACHE" >&2
    echo "       Install it first: /plugin install discord@claude-plugins-official" >&2
    echo "       Set FLEET_PLUGIN_CACHE if your cache lives somewhere else." >&2
    exit 1
fi

rc=0
for dir in "${dirs[@]}"; do
    version="$(basename "$dir")"
    target="$dir/server.ts"

    if [ ! -f "$target" ]; then
        echo "patch: $version has no server.ts, skipping" >&2
        continue
    fi

    if grep -q 'allowBots' "$target"; then
        echo "ok: $version already patched"
        [ "$version" = "$CUT_AGAINST" ] || echo "     (patch was cut against $CUT_AGAINST; this is $version)"
        continue
    fi

    if [ "$check_only" -eq 1 ]; then
        echo "UNPATCHED: $version" >&2
        rc=1
        continue
    fi

    # Dry run first so a failing hunk never leaves a half-patched server.ts.
    if ! patch -p1 -d "$dir" --dry-run --silent < "$PATCH" >/dev/null 2>&1; then
        echo "" >&2
        echo "patch: FAILED to apply to discord plugin $version" >&2
        echo "" >&2
        echo "  This patch was cut against $CUT_AGAINST. The plugin has moved the" >&2
        echo "  message handler and the patch needs regenerating against $version." >&2
        echo "  Do not start the fleet until it applies: unpatched, agents cannot" >&2
        echo "  wake each other and nothing logs a reason." >&2
        echo "" >&2
        echo "  Diagnose with:  patch -p1 -d $dir --dry-run < $PATCH" >&2
        echo "" >&2
        rc=1
        continue
    fi

    if ! patch -p1 -d "$dir" --silent < "$PATCH"; then
        echo "patch: $version dry run passed but the real apply failed" >&2
        rc=1
        continue
    fi

    if grep -q 'allowBots' "$target"; then
        echo "patched: $version"
    else
        echo "patch: $version reports applied but allowBots is not in server.ts" >&2
        rc=1
    fi
done

if [ "$rc" -ne 0 ] && [ "$check_only" -eq 1 ]; then
    echo "" >&2
    echo "Run bin/apply-plugin-patch.sh to fix. A plugin upgrade reverts this," >&2
    echo "so check it again after every upgrade." >&2
fi

exit "$rc"
