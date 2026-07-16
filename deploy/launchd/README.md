# Palinode as a macOS service (launchd)

Runs `palinode-api` and `palinode-watcher` as per-user LaunchAgents — started at
login, restarted on crash. This is the macOS sibling of `deploy/systemd/`
(Linux). If you'd rather not manage a venv + agents at all, `docker compose up`
at the repo root does the same job in containers — see the README's
**Running as a service** section.

## Install

The templates use `${VARIABLE}` placeholders, same convention as the systemd
templates. Fill them with `envsubst` (ships with gettext: `brew install gettext`):

```bash
export PALINODE_HOME=~/palinode-src       # your clone, with venv/ inside
export PALINODE_DATA_DIR=~/.palinode      # your memory dir
export OLLAMA_URL=http://127.0.0.1:11434
export EMBEDDING_MODEL=bge-m3
export API_PORT=6340

mkdir -p ~/Library/LaunchAgents ~/Library/Logs
for svc in api watcher; do
  envsubst < com.phasespace.palinode-$svc.plist.template \
    > ~/Library/LaunchAgents/com.phasespace.palinode-$svc.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.phasespace.palinode-$svc.plist
done
```

Check it worked:

```bash
launchctl list | grep palinode
palinode doctor
```

## Manage

```bash
# stop / start
launchctl bootout gui/$(id -u)/com.phasespace.palinode-api
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.phasespace.palinode-api.plist

# logs
tail -f ~/Library/Logs/palinode-api.log ~/Library/Logs/palinode-watcher.log
```

## Notes

- The API binds `127.0.0.1` in the template — loopback only. Edit the plist's
  `--host` argument if you genuinely want it on the network, and read the bind
  intent / token contract in `deploy/systemd/palinode-api.service.template`
  before you do.
- After editing a plist, `bootout` then `bootstrap` again — launchd does not
  re-read plists in place.
- Ollama has its own service story on macOS (the Ollama.app menu bar item, or
  `brew services start ollama`) — these agents assume it's already reachable
  at `OLLAMA_URL`.
