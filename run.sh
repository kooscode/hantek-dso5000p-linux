#!/bin/sh
# Start the hantek-dso GUI. On first run this creates a virtualenv in .venv and
# installs the app into it; after that it just starts the GUI.
# Extra arguments are passed to hantek-dso-gui.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/hantek-dso-gui ]; then
    echo "First run: creating .venv and installing hantek-dso (this takes a minute)..."
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -e '.[gui]'
fi
exec .venv/bin/hantek-dso-gui "$@"
