#!/bin/bash
# Free disk space on the GitHub-hosted runner (preinstalled toolchains this
# build never uses) and add a 16 GB swapfile for the big Rust link.
set -euo pipefail

sudo rm -rf /usr/share/dotnet /usr/share/ghc \
  /opt/hostedtoolcache/CodeQL /opt/hostedtoolcache/boost \
  /opt/hostedtoolcache/powershell /opt/hostedtoolcache/swift \
  /usr/local/share/boost /usr/local/share/powershell \
  /usr/share/powershell /opt/ghc /usr/local/lib/android 2>/dev/null || true

if [ ! -f /modbuild-swap.img ] && ! swapon --show | grep -q .; then
  sudo fallocate -l 16G /modbuild-swap.img || sudo dd if=/dev/zero of=/modbuild-swap.img bs=1M count=16384
  sudo chmod 600 /modbuild-swap.img
  sudo mkswap /modbuild-swap.img
  sudo swapon /modbuild-swap.img || true
fi

free -h || true
df -h / || true
