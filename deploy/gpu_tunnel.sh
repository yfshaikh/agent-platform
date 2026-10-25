#!/usr/bin/env bash
# The only path from the cluster to the GPU box: an SSH tunnel from this Mac, bound to loopback (not the LAN). Docker
# Desktop lets pods reach it as host.docker.internal:9400;
# on the box the node agent listens on 127.0.0.1 only, so nothing is exposed to the internet.
# Run and leave open:  bash deploy/gpu_tunnel.sh [ssh host, default nmc-gpu]
exec ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 127.0.0.1:9400:127.0.0.1:9400 "${1:-nmc-gpu}"
