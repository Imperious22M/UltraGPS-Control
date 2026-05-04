#!/bin/bash

# UltraGPS-Python Installation Script

set -e

echo "=== UltraGPS-Python Installer ==="
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

# Check for tkinter (required but often missing on Linux)
echo "Checking for tkinter..."
if ! $PYTHON_CMD -c "import tkinter" 2>/dev/null; then
    echo "Warning: tkinter is not installed."
    echo "On Debian/Ubuntu, install it with: sudo apt install python3-tk"
    echo "On Fedora, install it with: sudo dnf install python3-tkinter"
    echo "On Arch, install it with: sudo pacman -S tk"
    echo
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    echo "tkinter: OK"
fi

# Create virtual environment
VENV_DIR="venv"
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

# Activate virtual environment
echo "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install numpy scipy matplotlib filterpy

# Install local libraries
echo "Installing local libraries..."
pip install -e libs/ultragps_position
pip install -e libs/ultragps_server

echo
echo "=== Installation Complete ==="
echo
echo "To run the application:"
echo "  source venv/bin/activate"
echo "  python main.py"
echo
