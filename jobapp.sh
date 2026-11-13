#!/usr/bin/env bash
# Job App for macOS and Linux. Run from the folder you cloned:
#
#   ./jobapp.sh setup        create .venv and install everything (run again after git pull)
#   ./jobapp.sh signin       sign the agents in with your own Claude account or API key
#   ./jobapp.sh check        test this computer: browser, job feeds, agents
#   ./jobapp.sh              start Job App and open the dashboard (same as: run)
#   ./jobapp.sh demo         start it with made-up sample data
#   ./jobapp.sh where        show where your data is kept
#   ./jobapp.sh import DIR   copy your data from an older Job App folder
#   ./jobapp.sh update       git pull, then setup
#   ./jobapp.sh forget       delete everything Job App keeps about you
#
# Works with the bash that comes with macOS (3.2).
set -eu

cd "$(dirname "$0")"
VENV=".venv"
PY="$VENV/bin/python"

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

find_python() {
    for candidate in python3.12 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 14) else 1)' 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

need_setup() {
    if [ ! -x "$PY" ]; then
        echo "Job App is not set up yet. Run: ./jobapp.sh setup"
        exit 1
    fi
}

setup() {
    echo "Job App setup"
    echo
    if [ ! -x "$PY" ]; then
        if PYTHON="$(find_python)"; then
            echo "Creating a private Python environment in $VENV with $PYTHON ..."
            "$PYTHON" -m venv "$VENV"
        elif command -v uv >/dev/null 2>&1; then
            echo "No Python 3.12 or 3.13 found; using uv to get one ..."
            uv venv --seed --python 3.12 "$VENV"
        else
            echo "Python 3.12 or 3.13 was not found."
            echo "Install it, then run ./jobapp.sh setup again:"
            echo "  macOS:  brew install python@3.12    (or the installer from python.org)"
            echo "  Linux:  your package manager's python3.12 and python3.12-venv"
            echo "  or install uv (https://docs.astral.sh/uv/) and this script uses it."
            exit 1
        fi
    fi
    LOGS="$("$PY" -c 'from app.paths import default_paths; p = default_paths(); p.logs.mkdir(parents=True, exist_ok=True); print(p.logs)')"
    echo "Installing the packages. The first time this is a large download,"
    echo "because it includes the Claude runtime the agents use ..."
    "$PY" -m pip install --disable-pip-version-check --quiet --upgrade pip
    if ! "$PY" -m pip install --disable-pip-version-check -r requirements.txt --log "$LOGS/pip-install.log"; then
        echo
        echo "Setup did not finish. The reason is in the messages above and at the end of"
        echo "  $LOGS/pip-install.log"
        exit 1
    fi
    echo
    "$PY" -m app check --quick || true
    echo
    echo "Setup finished. Next:"
    echo "  ./jobapp.sh signin    sign in with your own Claude account or API key"
    echo "  ./jobapp.sh check     test the browser and the agents"
    echo "  ./jobapp.sh           start Job App"
}

COMMAND="${1:-run}"
if [ $# -gt 0 ]; then shift; fi

case "$COMMAND" in
    setup)
        setup
        ;;
    run)
        need_setup
        exec "$PY" -m app "$@"
        ;;
    demo)
        need_setup
        exec "$PY" -m app --demo "$@"
        ;;
    signin | check | where | import | forget)
        need_setup
        exec "$PY" -m app "$COMMAND" "$@"
        ;;
    update)
        git pull --ff-only
        setup
        ;;
    help | -h | --help)
        usage
        ;;
    *)
        echo "Unknown command: $COMMAND"
        echo
        usage
        exit 2
        ;;
esac
