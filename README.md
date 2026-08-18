# Repo Quality & Runnability Audit Dashboard

A privacy-preserving automated evaluation engine and web dashboard for auditing Git codebase quality, AST structural complexity, build runnability, and test discoverability.

## 🚀 Features

- **🌐 Modern Flask Web Dashboard**: Interactive UI with Chart.js charts and live log streaming.
- **🛡️ Privacy-Preserving Allowlist**: Emits zero raw code, zero secrets, zero author PII.
- **🔨 Deterministic Build Probe**: Resolves dependencies and queries runners (`pytest`, `jest`, `cargo`, `go test`) for test discovery.
- **🧬 AST Complexity & Modularity**: Tree-sitter powered function boundary, cyclomatic complexity, and AST depth calculations.
- **☁️ Cloud-Ready**: One-click deployable to Render via `render.yaml` and Gunicorn.

---

## 🏃 Local Quickstart

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start Web Dashboard
python run_dashboard.py

# 3. Open in Browser
# http://localhost:8000
```

---

## ☁️ Deployment on Render

1. Create a new **Blueprint** service on [Render](https://dashboard.render.com/).
2. Connect this repository (`render.yaml` will automatically configure the build & start commands).
3. Access your live dashboard!
