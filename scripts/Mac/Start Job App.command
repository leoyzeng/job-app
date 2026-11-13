#!/bin/bash
# Double-click in Finder to run: ./jobapp.sh
cd "$(dirname "$0")/../.." || exit 1
# Keep this window open while you use Job App. Press Ctrl+C here, or close the window, to stop it.
bash ./jobapp.sh
status=$?
if [ "$status" -ne 0 ]; then
    echo
    read -n 1 -s -r -p "Job App stopped with an error. Press any key to close this window."
fi
