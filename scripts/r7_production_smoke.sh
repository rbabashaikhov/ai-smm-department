#!/usr/bin/env bash
# R7.7 -- the production smoke after `mutations enable`, run from the
# owner's laptop through an SSH tunnel (docs/reports/smm-025d/r7/runbook.md).
#
#   scripts/r7_production_smoke.sh <ssh-host> <owner-email> [project-id]
#
# The owner's Control Center password is read from a RAM file the OWNER
# created beforehand (mode 600):
#
#   install -m 600 /dev/null /dev/shm/ai-smm-r7-owner.pw && read -r -s -p "CC owner password: " P \
#     && printf '%s' "$P" > /dev/shm/ai-smm-r7-owner.pw; unset P; echo
#
# The file is shredded after the run, whatever its outcome. The smoke
# (scripts/r7_rehearsal_client.py, phase `smoke`) runs in its own project
# (default r7-smoke, created on the first run), refuses a project with
# legacy publishing, schedules nothing and creates no publication. Prints
# statuses and codes only -- never a body, cookie, token or password.
set -uo pipefail

host="${1:?usage: $0 <ssh-host> <owner-email> [project-id]}"
email="${2:?usage: $0 <ssh-host> <owner-email> [project-id]}"
project="${3:-r7-smoke}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pw=/dev/shm/ai-smm-r7-owner.pw
port=18089
work="$(mktemp -d)"
chmod 700 "$work"
rc=99

[[ -f "$pw" ]] || { echo "STOP: $pw not found (create it as shown at the top of this script)"; exit 1; }
[[ "$(stat -c '%a' "$pw")" == 600 ]] || { echo "STOP: $pw must be mode 600"; shred -u -z "$pw"; exit 1; }

if ss -ltn | grep -q ":$port "; then
    echo "port $port is busy"
else
    ssh -N -o ExitOnForwardFailure=yes -L "127.0.0.1:$port:127.0.0.1:8088" "$host" </dev/null &
    tunnel=$!
    for _ in $(seq 10); do ss -ltn | grep -q "127.0.0.1:$port " && break; sleep 1; done
    if ss -ltn | grep -q "127.0.0.1:$port "; then
        echo "tunnel up on $port; smoke in project '$project' at $(date -u +%FT%TZ)"
        R7_PROJECT="$project" PYTHON_DOTENV_DISABLED=1 python3 -I "$repo/scripts/r7_rehearsal_client.py" \
            smoke "http://127.0.0.1:$port" "$email" "$pw" "$work/state.json"
        rc=$?
    else
        echo "the tunnel did not come up"
    fi
    kill "$tunnel" 2>/dev/null
fi

shred -u -z "$pw"; [[ -e "$pw" ]] && echo "PASSWORD FILE STILL PRESENT" || echo "password file shredded"
rm -rf "$work"
echo "SMOKE_RC=$rc"
exit "$rc"
