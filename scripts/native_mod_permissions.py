#!/usr/bin/env python3
"""Scan a native mod bundle's Rust sources for microphone use.

Horizon's native mods are not sandboxed, so the only honest signal the game
can show before install is a permission list derived from the sources. We look
for common microphone capture tokens; anything found marks the bundle with
"microphone" so the status document (and the UI) can say so. This is a
best-effort signal, not a proof: a mod can capture audio through other paths,
and plenty of legitimate audio code (synthesis, file playback) is not capture.
"""

import re

MICROPHONE_TOKENS = re.compile(
    r"\b(cpal|AudioRecord|AAudio|OpenSL|oboe)\b",
)


def scan_sources(texts):
    """texts: iterable of Rust source strings. Returns the permission list."""
    permissions = []
    for text in texts:
        if MICROPHONE_TOKENS.search(text or ""):
            permissions.append("microphone")
            break
    return permissions
