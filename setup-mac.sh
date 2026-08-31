#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

find_supported_python() {
  # MCP requires Python 3.10+, and this project is tested on 3.11+.
  local candidates=(
    python3.13
    python3.12
    python3.11
    /opt/homebrew/bin/python3
    /usr/local/bin/python3
    python3
  )

  local cmd
  for cmd in "${candidates[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      continue
    fi

    if "$cmd" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      echo "$cmd"
      return 0
    fi
  done

  return 1
}

PYTHON_BIN="$(find_supported_python || true)"

if [ -z "$PYTHON_BIN" ]; then
  echo "Python 3.11 or newer is required. macOS's default python3 is too old."
  if command -v brew >/dev/null 2>&1; then
    echo "Homebrew found. Installing the latest Python with: brew install python"
    brew install python
    PYTHON_BIN="$(find_supported_python || true)"
  fi
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "Could not find Python 3.11+."
  echo "Install Homebrew from https://brew.sh, then run: brew install python"
  echo "Or install Python 3.12+ from https://www.python.org/downloads/macos/"
  exit 1
fi

echo "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
echo "Recreating .venv with this Python..."
rm -rf .venv
"$PYTHON_BIN" -m venv .venv

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "Setup complete."
echo "Next steps:"
echo "  1. Install/start Ollama: https://ollama.com"
echo "  2. ollama pull qwen2.5:7b"
echo "  3. bash run-agent.sh"
