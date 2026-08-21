#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Virtual environment not found. Running setup-mac.sh first..."
  bash setup-mac.sh
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Recover from partial/incomplete setup where .venv exists but packages are missing.
if ! python -c "import httpx, mcp, bs4" >/dev/null 2>&1; then
  echo "Required Python packages are missing. Installing from requirements.txt..."
  python -m pip install -r requirements.txt
fi

python agent.py "$@"
