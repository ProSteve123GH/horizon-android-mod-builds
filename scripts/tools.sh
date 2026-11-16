#!/bin/bash
# One-time base dependencies for the build machine (base.yml only writes the
# cache; build.yml restores it and never runs this):
#   - rustup with the pinned toolchain (1.95.0) and the Android target
#   - Android command-line tools + NDK 28.2.13676358 under ~/android
#   - cargo-ndk and horizon-mixins (pinned rev) under ~/modbuild/cargo
#   - the ~/modbuild layout: source/ base/ cargo/ jobs/ result/
set -euo pipefail

export RUSTUP_HOME="$HOME/.rustup"
export CARGO_HOME="$HOME/modbuild/cargo"
export PATH="$CARGO_HOME/bin:$RUSTUP_HOME/toolchains/1.95.0-x86_64-unknown-linux-gnu/bin:$PATH"
MODBUILD="${MODBUILD:-$HOME/modbuild}"
ANDROID_HOME="$HOME/android"
NDK_VERSION="28.2.13676358"

mkdir -p "$MODBUILD"/{source,base,cargo,jobs,result}
mkdir -p "$ANDROID_HOME/cmdline-tools"

# --- rustup -----------------------------------------------------------------
if [ ! -x "$RUSTUP_HOME/toolchains/1.95.0-x86_64-unknown-linux-gnu/bin/cargo" ]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain 1.95.0 --profile minimal --target aarch64-linux-android
else
  # Already installed (e.g. cache restored): just make sure the target is there.
  "$RUSTUP_HOME/toolchains/1.95.0-x86_64-unknown-linux-gnu/bin/rustup" target add aarch64-linux-android || true
fi

# --- Android command-line tools + NDK ----------------------------------------
if [ ! -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then
  rm -rf "$ANDROID_HOME/cmdline-tools/cmdline-tools" /tmp/cmdtools.zip
  curl -sSL -o /tmp/cmdtools.zip \
    https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip
  unzip -q /tmp/cmdtools.zip -d "$ANDROID_HOME/cmdline-tools"
  mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
fi
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses >/dev/null || true
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --install "ndk;$NDK_VERSION"

# --- cargo helpers ------------------------------------------------------------
cargo install --locked cargo-ndk || true
cargo install --locked --git https://github.com/horizon-rust/horizon-mixins \
  --rev ddd9700b21997c95d95ac6ee5ecafe3830cf622a horizon-mixins-cli

echo "tools.sh: base dependencies ready"
