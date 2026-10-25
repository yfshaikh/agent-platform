#!/usr/bin/env bash
# Install (or update) the GPU node agent on the Brev box and start it as a systemd service.
# The token goes from the cluster Secret to the box over SSH stdin: never printed, never in a command line.
# Run from agent-platform/:  bash deploy/gpu_node_setup.sh [ssh host, default nmc-gpu]
set -euo pipefail
HOST="${1:-nmc-gpu}"
rsync -a --delete gpu-node/ "$HOST":~/gpu-node/
kubectl --context kind-f1 -n platform get secret gpu-node-token -o jsonpath='{.data.NODE_TOKEN}' | base64 -d |
  ssh "$HOST" 'umask 077; { printf "NODE_TOKEN="; cat; echo; } > ~/gpu-node/.env'
ssh "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
sudo tee /etc/systemd/system/gpu-node.service >/dev/null <<UNIT
[Unit]
Description=GPU node agent
After=docker.service
[Service]
User=$(whoami)
WorkingDirectory=$HOME/gpu-node
EnvironmentFile=$HOME/gpu-node/.env
ExecStart=/usr/bin/python3 $HOME/gpu-node/node_agent.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-node
sudo systemctl restart gpu-node
docker pull -q pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime >/dev/null &  # the job image (several GB), in the background
sleep 2 && systemctl is-active gpu-node
REMOTE
