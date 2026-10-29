# Grok Bot harness

Runs a hosted Grok Bot agent (the Electron `grok-bot` desktop app) on a Linux
box and lets it talk to a Discord-based agent team without ever holding a
credential. The app executes shell commands on the box under its own uid, so
the design assumes that uid is hostile and confines it.

## Pieces

- `broker.py`: a daemon under its own uid (`AGENT-broker`). It holds the Discord
  token, listens on a unix socket, checks the peer uid with SO_PEERCRED, caps
  outgoing volume, and can only post to channels listed in its config. Config
  is JSON, see `broker.example.json`. The optional key `socket_group` chowns
  the socket to that group after it is bound; the broker must be a member of it.
- `team.py`: the CLI the agent runs, installed as `/usr/local/bin/team`. It finds
  the socket through `TEAM_SOCKET`, default `/run/grokbot-broker/team.sock`
  (the app unit sets it to `/run/AGENT-broker/team.sock`).
- `../../bin/grokbot_wake.py`: the watcher, run by the operator, that wakes the
  agent when it is mentioned. It reads the broker's `tickets.json`.
- `install/`: templates for systemd units and an nftables policy, `render.sh`
  to fill in the agent name, and `verify-confinement.sh` to prove the result.
  Templates use `__AGENT__` for the agent name.

## Trust boundaries

```
  Discord  <--- token, HTTPS --- AGENT-broker (uid AGENT-broker)
                                     |
                       unix socket 0660, group AGENT
                       checked with SO_PEERCRED
                                     |
  internet <--- tcp 80/443 --- AGENT (uid AGENT: grok-bot, its shell, team)
  (nft egress: DNS via 127.0.0.53, web only; no loopback, no LAN)

  operator uid: reads tickets.json via an ACL; nothing else is shared.
  AGENT cannot read the operator's files, /etc/AGENT-broker or
  /var/lib/AGENT-broker, and cannot use cron, at, ssh or sudo.
```

## Provisioning

Run as root on the box, from a checkout of this repo. Set the agent name
(lowercase letters, digits and hyphens, starting with a letter) and the
operator account that runs the watcher:

```
AGENT=example
OPERATOR=youruser
```

The broker's user name is `AGENT-broker`, and Linux user names stop at 32
characters, so AGENT is limited to 24 characters (render.sh and the verify
script enforce it).

1. Render the templates.

```
OUT=$(mktemp -d)
harness/grokbot/install/render.sh "$AGENT" "$OUT"
```

2. Create the users. The agent gets a bash shell (the app runs commands through
it) and a locked password. The broker cannot log in.

```
useradd --system --user-group --create-home --home-dir "/home/$AGENT" --shell /bin/bash "$AGENT"
chmod 0700 "/home/$AGENT"
passwd -l "$AGENT"
useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$AGENT-broker"
```

3. Deny the agent cron and at. If `/etc/cron.allow` or `/etc/at.allow` exists it
takes precedence, and the agent must not be in it.

```
touch /etc/cron.deny /etc/at.deny
grep -qx "$AGENT" /etc/cron.deny || echo "$AGENT" >> /etc/cron.deny
grep -qx "$AGENT" /etc/at.deny || echo "$AGENT" >> /etc/at.deny
```

4. Deny ssh. The service is `ssh` on Debian and Ubuntu, `sshd` elsewhere.

```
echo "DenyUsers $AGENT" > "/etc/ssh/sshd_config.d/50-deny-$AGENT.conf"
sshd -t && systemctl reload ssh
```

5. Install the broker and the client.

```
install -d -m 0755 "/usr/local/lib/$AGENT-broker"
install -m 0644 harness/grokbot/broker.py "/usr/local/lib/$AGENT-broker/broker.py"
install -m 0755 harness/grokbot/team.py /usr/local/bin/team
```

6. Create the config directory and the token. The token is read without
echo and goes through a pipe, so it never appears on a command line or in
shell history.

```
install -d -m 0750 -o "$AGENT-broker" -g "$AGENT-broker" "/etc/$AGENT-broker"
install -m 0640 -o root -g "$AGENT-broker" harness/grokbot/broker.example.json "/etc/$AGENT-broker/broker.json"
read -rsp 'Discord bot token: ' TOKEN; echo
printf '%s' "$TOKEN" | install -m 0600 -o "$AGENT-broker" -g "$AGENT-broker" /dev/stdin "/etc/$AGENT-broker/token"
unset TOKEN
```

Edit `/etc/$AGENT-broker/broker.json`: your channel and agent ids, plus these
values.

```
"token_file":    "/etc/AGENT-broker/token"
"allowed_uid":   <output of: id -u AGENT>
"socket":        "/run/AGENT-broker/team.sock"
"socket_group":  "AGENT"
"state_dir":     "/var/lib/AGENT-broker"
"log_file":      "/var/log/AGENT-broker/broker.log"
```

7. Create the state directory and let the operator's watcher read tickets. The
operator can traverse the directory and read files in it, but not list it. This
needs the `acl` package.

```
install -d -m 0750 -o "$AGENT-broker" -g "$AGENT-broker" "/var/lib/$AGENT-broker"
setfacl -m "u:$OPERATOR:x" "/var/lib/$AGENT-broker"
setfacl -d -m "u:$OPERATOR:r" "/var/lib/$AGENT-broker"
```

8. Install the units and the firewall, then check the firewall file.

```
install -m 0644 -t /etc/systemd/system "$OUT/$AGENT-broker.service" "$OUT/$AGENT-egress.service" "$OUT/$AGENT-app.service"
install -d -m 0755 "/etc/$AGENT-firewall"
install -m 0644 "$OUT/egress.nft" "/etc/$AGENT-firewall/egress.nft"
nft -c -f "/etc/$AGENT-firewall/egress.nft" && echo nft ok
```

9. Start the firewall and the broker. The app comes later, once `grok-bot` is
installed for the agent.

```
systemctl daemon-reload
systemctl enable --now "$AGENT-egress.service" "$AGENT-broker.service"
```

10. Verify. Every line must be PASS (SKIP means the box lacks the tool or path
for that check). The script reads `VERIFY_OPERATOR_HOME` (default `/home/ops`)
and `VERIFY_PRIVATE_DIRS` (default `/opt/web /home/ops`); set them to match the
box.

```
VERIFY_OPERATOR_HOME="/home/$OPERATOR" VERIFY_PRIVATE_DIRS="/home/$OPERATOR" \
  harness/grokbot/install/verify-confinement.sh "$AGENT"
```

11. When the app is installed, start it and verify again from inside its
namespace.

```
systemctl enable --now "$AGENT-app.service"
harness/grokbot/install/verify-confinement.sh "$AGENT" --app
```

## Notes

- The egress unit has no `ExecStop`. Stopping it does not open the firewall,
  and the app unit is bound to it, so the app stops with it.
- To change the firewall, edit the file, run `nft -c -f` on it, then
  `systemctl restart "$AGENT-egress.service"`. The file replaces the table in
  one transaction. The unit's own `ExecStartPre` runs the same check first.
- The app unit hides paths that do not exist on every box with a leading `-`.
  Edit its `InaccessiblePaths` for the directories the agent must not see on
  yours.
- The verify script probes loopback and the LAN gateway with a connect()
  classifier run as the agent. A drop shows as an immediate EPERM or a timeout
  ("blocked"). "refused" means the packet got through, so it fails. A control
  probe run as root must reach 127.0.0.1:22 first, or that check is skipped.
- The agent is IPv4 only: the policy drops all its IPv6 traffic, and also
  drops anything addressed to one of the box's own addresses.
- DNS assumes the systemd-resolved stub at `127.0.0.53`. On a box without it,
  the agent will not resolve names until the DNS rule is pointed at the right
  resolver.
- If the app won't start: check journalctl for the sandbox error first; drop
  `ProcSubset=pid` before anything else; `--no-sandbox` only with the logged
  error.

## Firewall persistence

The rules live only in the kernel. A `nft flush ruleset`, or restarting
`nftables.service` with a stock config, removes the table while the app keeps
running, and the app is then unconfined on the network. The app unit refuses to
start without the table (an `ExecStartPre` checks), but that does not stop a
running app. After any firewall change, run
`systemctl restart "$AGENT-egress" "$AGENT-app"`, then run
`install/verify-confinement.sh "$AGENT" --app`.

## Removing it

Stop the units, then delete the firewall table by hand, because stopping the
egress unit leaves the rules in place.

```
systemctl disable --now "$AGENT-app.service" "$AGENT-broker.service" "$AGENT-egress.service"
nft delete table inet "${AGENT}_egress"
rm -f /etc/systemd/system/"$AGENT"-{app,broker,egress}.service
rm -rf "/etc/$AGENT-firewall"
systemctl daemon-reload
```

Remove the ssh, cron and at denials.

```
rm -f "/etc/ssh/sshd_config.d/50-deny-$AGENT.conf"
sshd -t && systemctl reload ssh
sed -i "/^$AGENT\$/d" /etc/cron.deny /etc/at.deny
```

Delete the users and data. Kill the agent's leftover processes first. Keep
`/usr/local/bin/team` if another agent on the box uses it.

```
pkill -u "$AGENT"
userdel -r "$AGENT"
userdel "$AGENT-broker"
rm -rf "/etc/$AGENT-broker" "/var/lib/$AGENT-broker" "/var/log/$AGENT-broker" "/usr/local/lib/$AGENT-broker"
rm -f /usr/local/bin/team
```

Then revoke the Discord bot token in the Discord developer portal. Deleting the
file does not make a token that was ever on this box safe to keep.
