#!/usr/bin/env bash
# 物理/Forge 管线：直读 iCloud，零 pull
set -euo pipefail
export FOLIATION_ICLOUD_ROOT="${FOLIATION_ICLOUD_ROOT:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/Foliation-Engine-Archive}"
export FOLIATION_USE_ICLOUD=1
export PASS_V1_ROOT="${PASS_V1_ROOT:-$HOME/Desktop/foliation-pass-v1}"
export CHIP_PHYSICS_SIM_SKIP="${CHIP_PHYSICS_SIM_SKIP:-1}"
