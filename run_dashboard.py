#!/usr/bin/env python3
"""
run_dashboard.py -- Convenient launcher for the Repo Quality Web Dashboard
"""

import sys
import os
from pathlib import Path

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add current directory to path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from web_dashboard.app import app

def main():
    port = int(os.environ.get("PORT", 8000))
    print("=" * 60)
    print("  [*] REPO QUALITY & RUNNABILITY AUDIT DASHBOARD")
    print("=" * 60)
    print(f"  * Web Dashboard URL: http://localhost:{port}")
    print(f"  * Core Measurer:      {BASE_DIR / 'measure.py'}")
    print(f"  * Scratch / Runs Dir: {BASE_DIR / 'scratch' / 'dashboard_runs'}")
    print("=" * 60)
    print("  Press Ctrl+C in terminal to stop server.\n")

    app.run(host="127.0.0.1", port=port, debug=False)

if __name__ == "__main__":
    main()
