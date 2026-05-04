#!/bin/bash
# Start the Propeller Teststand web server
cd "$(dirname "$0")"
bash setup_ap.sh
python3 server.py
