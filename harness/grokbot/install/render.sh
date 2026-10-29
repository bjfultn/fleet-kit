#!/usr/bin/env bash
# Usage: render.sh AGENT OUTDIR [PRIVATE_PATH...]
# Writes the install templates into OUTDIR with __AGENT__ replaced by AGENT,
# under the names they are installed as.
#
# Each PRIVATE_PATH is an absolute directory the agent must not see on this box
# (the operator's home, the sites root, ...). It becomes an InaccessiblePaths=-
# line in the app unit; the leading "-" keeps a path that does not exist from
# failing the unit. Pass the same list to verify-confinement.sh through
# VERIFY_PRIVATE_DIRS.
set -eu

if [ "$#" -lt 2 ]; then
    echo "usage: $0 AGENT OUTDIR [PRIVATE_PATH...]" >&2
    exit 2
fi
agent=$1
out=$2
shift 2
if ! [[ $agent =~ ^[a-z][a-z0-9-]{1,23}$ ]]; then
    echo "render: AGENT must match ^[a-z][a-z0-9-]{1,23}\$" >&2
    exit 2
fi

private_lines=
for path in "$@"; do
    # Absolute, no whitespace or shell/systemd specials: it goes into a unit file.
    if ! [[ $path =~ ^/[A-Za-z0-9._/+@-]*$ ]] || [ "$path" = / ]; then
        echo "render: PRIVATE_PATH must be an absolute path of plain characters: $path" >&2
        exit 2
    fi
    private_lines+="InaccessiblePaths=-$path"$'\n'
done
if [ -z "$private_lines" ]; then
    private_lines="# No PRIVATE_PATH given to render.sh: nothing extra is hidden from the agent."$'\n'
fi

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$out"

render() {  # template  output-name
    local line
    : > "$out/$2"
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$line" = "__PRIVATE_PATHS__" ]; then
            printf '%s' "$private_lines" >> "$out/$2"
        else
            printf '%s\n' "${line//__AGENT__/$agent}" >> "$out/$2"
        fi
    done < "$here/$1"
    if grep -qE '__AGENT__|__PRIVATE_PATHS__' "$out/$2"; then
        echo "render: placeholder left in $2" >&2
        exit 1
    fi
}

render broker.service "$agent-broker.service"
render grokbot-app.service "$agent-app.service"
render egress.service "$agent-egress.service"
render egress.nft egress.nft
echo "rendered 4 files into $out"
if [ "$#" -gt 0 ]; then
    echo "verify with: VERIFY_PRIVATE_DIRS=\"$*\" verify-confinement.sh $agent"
fi
