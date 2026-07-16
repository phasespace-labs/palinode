#!/bin/sh
# Container entrypoint: make a bind-mounted memory dir work on first run,
# then exec the requested service (api or watcher).
#
# Palinode treats the memory dir as a git repo (every save is committed).
# The app itself never runs `git init` — it warns once and saves without
# version control. Inside a container that warning is easy to miss, so the
# entrypoint closes the gap: init an empty /data and give the repo a local
# identity commits can use. An existing repo is left untouched.
set -eu

PALINODE_DIR="${PALINODE_DIR:-/data}"

mkdir -p "$PALINODE_DIR"

# The bind mount is typically owned by the host user while this process runs
# as root — git's dubious-ownership protection would then reject every command
# against the repo (surfacing as "fatal: not in a git directory" from
# `git config`, and silently un-versioned saves from the app). Mark it safe in
# the CONTAINER's gitconfig only — this never writes to the mounted volume.
git config --global --add safe.directory "$PALINODE_DIR"

if [ ! -d "$PALINODE_DIR/.git" ]; then
    git init --quiet "$PALINODE_DIR"
    echo "palinode-entrypoint: initialized git repo in $PALINODE_DIR"
fi

# Repo-local identity only — never touch global config on a mounted volume.
if ! git -C "$PALINODE_DIR" config user.email >/dev/null 2>&1; then
    git -C "$PALINODE_DIR" config user.name "palinode"
    git -C "$PALINODE_DIR" config user.email "palinode@localhost"
fi

exec "$@"
