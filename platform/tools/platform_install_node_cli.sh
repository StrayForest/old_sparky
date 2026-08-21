#!/usr/bin/env bash
set -euo pipefail

NODE_HOME="${PLATFORM_NODE_HOME:-/opt/oldsparky/platform/shared/node-current}"
INSTALL_DIR="${PLATFORM_NODE_CLI_DIR:-/usr/local/bin}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

if [[ ! -x "$NODE_HOME/bin/node" ]]; then
  echo "Node runtime is missing: $NODE_HOME/bin/node" >&2
  exit 1
fi

NODE_MAJOR="$("$NODE_HOME/bin/node" -p "process.versions.node.split('.')[0]")"
if [[ "$NODE_MAJOR" != "26" ]]; then
  echo "Node 26 is required; got $("$NODE_HOME/bin/node" --version)." >&2
  exit 1
fi

install -d -m 0755 "$INSTALL_DIR"
for command_name in node npm npx; do
  if [[ ! -x "$NODE_HOME/bin/$command_name" ]]; then
    echo "Required command is missing: $NODE_HOME/bin/$command_name" >&2
    exit 1
  fi
  ln -sfn "$NODE_HOME/bin/$command_name" "$INSTALL_DIR/$command_name"
done

echo "Node CLI activated: $($INSTALL_DIR/node --version), npm $($INSTALL_DIR/npm --version)"
