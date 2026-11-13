#!/bin/bash
# Double-click in Finder to run: ./jobapp.sh setup
cd "$(dirname "$0")/../.." || exit 1
bash ./jobapp.sh setup
echo
read -n 1 -s -r -p "Press any key to close this window."
echo
