#!/usr/bin/env bash
# Usage: render.sh AGENT OUTDIR
# Writes the install templates into OUTDIR with __AGENT__ replaced by AGENT,
# under the names they are installed as.
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 AGENT OUTDIR" >&2
    exit 2
fi
agent=$1
out=$2
if ! [[ $agent =~ ^[a-z][a-z0-9-]{1,30}$ ]]; then
    echo "render: AGENT must match ^[a-z][a-z0-9-]{1,30}\$" >&2
    exit 2
fi

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$out"

render() {  # template  output-name
    sed "s/__AGENT__/$agent/g" "$here/$1" > "$out/$2"
    if grep -q '__AGENT__' "$out/$2"; then
        echo "render: placeholder left in $2" >&2
        exit 1
    fi
}

render broker.service "$agent-broker.service"
render grokbot-app.service "$agent-app.service"
render egress.service "$agent-egress.service"
render egress.nft egress.nft
echo "rendered 4 files into $out"
