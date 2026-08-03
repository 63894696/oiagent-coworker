# Build notes

The Tauri app uses LGPL-2.1 for the systray helper on Linux. We load
it dynamically, not statically. See scripts/license_lint.py policy.

SPDX: MIT