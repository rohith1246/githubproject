#!/usr/bin/env python3
"""
wsgi.py -- Production WSGI entrypoint for Render and Gunicorn
"""

import sys
from pathlib import Path

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from web_dashboard.app import app

if __name__ == "__main__":
    app.run()
