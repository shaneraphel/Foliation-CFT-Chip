#!/usr/bin/env bash
# 物理/Forge 管线：直读 iCloud + GitHub CFT 芯片（零 pull）
set -euo pipefail
export FOLIATION_ICLOUD_ROOT="${FOLIATION_ICLOUD_ROOT:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/Foliation-Engine-Archive}"
export FOLIATION_USE_ICLOUD=1
export FOLIATION_CFT_ROOT="${FOLIATION_CFT_ROOT:-$HOME/Foliation-CFT-Chip}"
export PASS_V1_ROOT="${PASS_V1_ROOT:-$HOME/Desktop/foliation-pass-v1}"
# 物理 A/B 芯片门禁默认开启；仅当调用者显式导出 CHIP_PHYSICS_SIM_SKIP=1 时跳过。
