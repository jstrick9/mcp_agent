#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
source .venv/bin/activate
python agent.py "$@"
