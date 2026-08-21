#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it with: brew install python"
  exit 1
fi

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "Setup complete."
echo "Next steps:"
echo "  1. Install/start Ollama: https://ollama.com"
echo "  2. ollama pull qwen2.5:7b"
echo "  3. source .venv/bin/activate"
echo "  4. python agent.py"
