#!/usr/bin/env python3
"""
app.py -- Flask Web Dashboard Backend for repo-quality-evaluation-measure-ext
Provides REST APIs and UI serving for repository quality audits.
Includes disk-persisted state to support multi-worker environments (Render/Gunicorn).
"""

import os
import sys
import json
import uuid
import copy
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, abort

# Set up paths
BASE_DIR = Path(__file__).resolve().parent.parent
MEASURE_PY = BASE_DIR / "measure.py"
SCRATCH_DIR = BASE_DIR / "scratch" / "dashboard_runs"
CLONES_DIR = BASE_DIR / "scratch" / "temp_clones"

# Ensure scratch directories exist
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
CLONES_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")

JOBS = {}
JOBS_LOCK = threading.Lock()


def save_job_meta(job_id, job_entry):
    job_dir = SCRATCH_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    meta_path = job_dir / "job_meta.json"
    try:
        with JOBS_LOCK:
            meta_copy = copy.deepcopy(job_entry)
        meta_copy["logs"] = []  # stored separately in audit.log
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_copy, f, indent=2)
    except Exception as e:
        print(f"Error saving job meta: {e}")


def load_job(job_id):
    """Load job state safely from in-memory cache or disk (for multi-worker sync)."""
    job = None
    with JOBS_LOCK:
        if job_id in JOBS:
            job = copy.deepcopy(JOBS[job_id])

    job_dir = SCRATCH_DIR / job_id
    meta_path = job_dir / "job_meta.json"
    log_path = job_dir / "audit.log"

    if not job and meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            pass

    if not job:
        return None

    # Load logs from audit.log
    logs = []
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                logs = [line.rstrip() for line in f.readlines() if line.strip()]
        except Exception:
            pass
    job["logs"] = logs

    # Ensure results are populated if files exist
    results = job.get("results") or {}
    measurement_file = job_dir / "measurement.json"
    codebase_file = job_dir / "codebase_repos.json"
    mining_file = job_dir / "codebase_repo_mining.json"

    if "measurement" not in results and measurement_file.exists():
        try:
            with open(measurement_file, "r", encoding="utf-8") as f:
                results["measurement"] = json.load(f)
        except Exception:
            pass

    if "codebase_repos" not in results and codebase_file.exists():
        try:
            with open(codebase_file, "r", encoding="utf-8") as f:
                results["codebase_repos"] = json.load(f)
        except Exception:
            pass

    if "mining" not in results and mining_file.exists():
        try:
            with open(mining_file, "r", encoding="utf-8") as f:
                results["mining"] = json.load(f)
        except Exception:
            pass

    job["results"] = results
    if measurement_file.exists() and job.get("status") in ("measuring", "queued", "cloning"):
        job["status"] = "completed"

    return job


def run_measurement_thread(job_id, target_path, is_temp_clone, options):
    job_dir = SCRATCH_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log_file = job_dir / "audit.log"

    def log(msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {msg}"
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["logs"].append(formatted)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")

    try:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "measuring"
        save_job_meta(job_id, JOBS.get(job_id, {}))

        log(f"Starting measurement on target: {target_path}")
        log(f"Options: {options}")

        # Assemble CLI arguments for measure.py
        cmd = [
            sys.executable,
            "-u",
            str(MEASURE_PY),
            str(target_path),
            "--out", str(job_dir),
            "--sink", "json",
            "--review"
        ]

        build_mode = options.get("build_mode", "discover")
        cmd.extend(["--build", build_mode])

        if options.get("no_llm", True):
            cmd.append("--no-llm")
        elif options.get("provider"):
            cmd.extend(["--provider", options["provider"]])
            if options.get("model"):
                cmd.extend(["--model", options["model"]])

        if options.get("jobs"):
            cmd.extend(["--jobs", str(options["jobs"])])

        if options.get("budget_seconds"):
            cmd.extend(["--budget-seconds", str(options["budget_seconds"])])

        log(f"Executing command: {' '.join(cmd)}")

        # Execute subprocess and capture live output
        process = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace"
        )

        while True:
            line = process.stdout.readline()
            if line:
                log(line.rstrip())
            elif process.poll() is not None:
                # Main process has exited, drain remaining lines
                try:
                    for remaining in process.stdout.readlines():
                        if remaining.strip():
                            log(remaining.rstrip())
                except Exception:
                    pass
                break
            else:
                time.sleep(0.05)

        try:
            process.stdout.close()
        except Exception:
            pass

        try:
            return_code = process.wait(timeout=5)
        except Exception:
            return_code = 0

        if return_code != 0:
            log(f"Process exited with code: {return_code}")

        # Parse generated results
        results = {}
        measurement_file = job_dir / "measurement.json"
        codebase_file = job_dir / "codebase_repos.json"
        mining_file = job_dir / "codebase_repo_mining.json"

        if measurement_file.exists():
            try:
                with open(measurement_file, "r", encoding="utf-8") as f:
                    results["measurement"] = json.load(f)
            except Exception as e:
                log(f"Warning: Failed to parse measurement.json: {e}")

        if codebase_file.exists():
            try:
                with open(codebase_file, "r", encoding="utf-8") as f:
                    results["codebase_repos"] = json.load(f)
            except Exception as e:
                log(f"Warning: Failed to parse codebase_repos.json: {e}")

        if mining_file.exists():
            try:
                with open(mining_file, "r", encoding="utf-8") as f:
                    results["mining"] = json.load(f)
            except Exception as e:
                log(f"Warning: Failed to parse codebase_repo_mining.json: {e}")

        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["results"] = results
                JOBS[job_id]["status"] = "completed" if (measurement_file.exists() or return_code == 0) else "failed"
                if JOBS[job_id]["status"] == "completed":
                    log("Measurement completed successfully.")
                else:
                    JOBS[job_id]["error"] = f"Process exited with return code {return_code}"
                save_job_meta(job_id, JOBS[job_id])

    except Exception as e:
        log(f"Fatal execution error: {e}")
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = str(e)
                save_job_meta(job_id, JOBS[job_id])

    finally:
        if is_temp_clone and target_path.exists():
            try:
                log("Cleaning up temporary clone directory...")
                shutil.rmtree(target_path, ignore_errors=True)
            except Exception as e:
                log(f"Cleanup note: {e}")


def clone_and_run(job_id, git_url, options):
    job_clone_dir = CLONES_DIR / job_id
    job_clone_dir.mkdir(parents=True, exist_ok=True)
    log_file = (SCRATCH_DIR / job_id) / "audit.log"
    (SCRATCH_DIR / job_id).mkdir(parents=True, exist_ok=True)

    def log(msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {msg}"
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["logs"].append(formatted)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")

    try:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "cloning"
        save_job_meta(job_id, JOBS.get(job_id, {}))

        log(f"Cloning remote repository: {git_url} ...")
        clone_cmd = ["git", "clone", "--depth", "100", git_url, str(job_clone_dir)]
        clone_proc = subprocess.run(
            clone_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

        if clone_proc.returncode != 0:
            err_msg = clone_proc.stderr.strip() or "Git clone failed"
            log(f"Git clone error: {err_msg}")
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["status"] = "failed"
                    JOBS[job_id]["error"] = err_msg
            save_job_meta(job_id, JOBS.get(job_id, {}))
            return

        log("Git clone successful. Proceeding to measurement...")
        run_measurement_thread(job_id, job_clone_dir, is_temp_clone=True, options=options)

    except Exception as e:
        log(f"Clone error: {e}")
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = str(e)
        save_job_meta(job_id, JOBS.get(job_id, {}))


# ---------------- Routes ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/api/audit", methods=["POST"])
def start_audit():
    data = request.get_json() or {}
    target_type = data.get("target_type", "local")
    target_val = (data.get("target") or "").strip()

    if not target_val:
        return jsonify({"error": "Target repository path or URL is required."}), 400

    job_id = str(uuid.uuid4())[:8]
    options = {
        "build_mode": data.get("build_mode", "discover"),
        "no_llm": data.get("no_llm", True),
        "provider": data.get("provider", "claude"),
        "model": data.get("model", ""),
        "jobs": data.get("jobs", 4),
        "budget_seconds": data.get("budget_seconds", 900)
    }

    job_entry = {
        "id": job_id,
        "created_at": datetime.now().isoformat(),
        "status": "queued",
        "target": target_val,
        "target_type": target_type,
        "options": options,
        "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] Job queued with ID: {job_id}"],
        "results": {},
        "error": None
    }

    with JOBS_LOCK:
        JOBS[job_id] = job_entry
    save_job_meta(job_id, job_entry)

    # Also start the audit log file
    job_dir = SCRATCH_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with open(job_dir / "audit.log", "w", encoding="utf-8") as f:
        f.write(job_entry["logs"][0] + "\n")

    if target_type == "remote":
        t = threading.Thread(target=clone_and_run, args=(job_id, target_val, options), daemon=True)
        t.start()
    else:
        local_path = Path(target_val).resolve()
        if not local_path.exists() or not local_path.is_dir():
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = f"Local directory '{target_val}' does not exist."
            save_job_meta(job_id, JOBS.get(job_id, {}))
            return jsonify({"job_id": job_id, "status": "failed", "error": f"Path '{target_val}' not found."}), 400

        t = threading.Thread(target=run_measurement_thread, args=(job_id, local_path, False, options), daemon=True)
        t.start()

    return jsonify({"job_id": job_id, "status": "queued", "message": "Audit started successfully."})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    try:
        job = load_job(job_id)
        if not job:
            return jsonify({"error": "Job not found", "job_id": job_id}), 404
        return jsonify(job)
    except Exception as e:
        return jsonify({"error": str(e), "job_id": job_id}), 500


@app.route("/api/history", methods=["GET"])
def get_history():
    history = []
    # Scan scratch directory for all jobs
    if SCRATCH_DIR.exists():
        for job_folder in SCRATCH_DIR.iterdir():
            if job_folder.is_dir():
                meta_file = job_folder / "job_meta.json"
                if meta_file.exists():
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                            history.append({
                                "id": meta.get("id", job_folder.name),
                                "created_at": meta.get("created_at", ""),
                                "status": meta.get("status", "unknown"),
                                "target": meta.get("target", ""),
                                "target_type": meta.get("target_type", "")
                            })
                    except Exception:
                        pass
    history.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify(history)


@app.route("/api/download/<job_id>/<file_type>", methods=["GET"])
def download_artifact(job_id, file_type):
    job_dir = SCRATCH_DIR / job_id
    if not job_dir.exists():
        abort(404, description="Job artifact directory not found")

    file_mapping = {
        "measurement_json": ("measurement.json", "measurement.json", "application/json"),
        "codebase_repos_json": ("codebase_repos.json", "codebase_repos.json", "application/json"),
        "codebase_repos_csv": ("codebase_repos.csv", "codebase_repos.csv", "text/csv"),
        "mining_json": ("codebase_repo_mining.json", "codebase_repo_mining.json", "application/json"),
        "log": ("audit.log", "audit.log", "text/plain")
    }

    if file_type not in file_mapping:
        abort(400, description="Invalid file type requested")

    filename, download_name, mime_type = file_mapping[file_type]
    file_path = job_dir / filename

    if not file_path.exists():
        abort(404, description=f"File {filename} was not generated in this run")

    return send_file(
        str(file_path),
        as_attachment=True,
        download_name=download_name,
        mimetype=mime_type
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 Starting Repo Quality Web Dashboard on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
