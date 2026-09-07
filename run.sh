#!/bin/env bash

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Globals
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
SCRIPT_RUNNING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_ENV_NAME=".venv"
PYTHON_MAIN="graphics.py"

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Start the Control GUI
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
echo "Script Running Dir: $SCRIPT_RUNNING_DIR"
"$SCRIPT_RUNNING_DIR/$PYTHON_ENV_NAME/bin/python" "$SCRIPT_RUNNING_DIR/$PYTHON_MAIN" "$@"
