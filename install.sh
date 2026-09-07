#!/bin/bash

# UltraGPS-Control Installation Script

set -e

# requirements.txt uses paths relative to the project root, so always run from there.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== UltraGPS-Control Installer ==="
echo

# Check Python version
PYTHON_CMD=""
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "Error: Python not found. Please install Python 3.11 or later."
    exit 1
fi

PYTHON_VERSION=$($PYTHON_CMD -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.minor)')

echo "Found Python $PYTHON_VERSION"

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    echo "Error: Python 3.11 or later is required (for tomllib support)."
    echo "Current version: $PYTHON_VERSION"
    exit 1
fi

# Create virtual environment
VENV_DIR=".venv"
if [ -d "$VENV_DIR" ]; then
    echo "Virtual environment already exists."
    read -p "Recreate it? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$VENV_DIR"
        echo "Creating virtual environment..."
        $PYTHON_CMD -m venv "$VENV_DIR"
    fi
else
    echo "Creating virtual environment..."
    $PYTHON_CMD -m venv "$VENV_DIR"
fi

# Upgrade pip
echo "Upgrading pip..."
"$VENV_DIR/bin/pip" install --upgrade pip

# Install third-party dependencies and the five local libraries.
# The -e entries in requirements.txt cover libs/ultragps_{client,position,
# calibration,barrier,server}; pip resolves ultragps-calibration's dependency
# on ultragps-client from the local editable install in the same run.
echo "Installing dependencies and local libraries..."
"$VENV_DIR/bin/pip" install -r requirements.txt

# Sanity check: the GUI must be able to import every module it needs.
echo "Verifying installation..."
MPLBACKEND=Agg "$VENV_DIR/bin/python" -c "
import PyQt6, matplotlib, numpy, scipy
import ultragps_client, ultragps_position, ultragps_calibration
import ultragps_barrier, ultragps_server
print('All modules import correctly.')
"

echo
echo "=== Installation Complete ==="
echo
echo "To run the application:"
echo "  ./run.sh [--ip <server-address>]"
echo
