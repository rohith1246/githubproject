#!/usr/bin/env python3
"""
agent_io.py — the one way this skill talks to a headless provider agent.

Library module, not a CLI. Three callers need the same three things — spawn one
headless turn against a repo, pull JSON out of whatever the model actually said,
and read a repo's HEAD sha — and each had grown its own copy with slightly
different error handling and a different `--allowedTools` spelling. This is that
code, once.

Three providers are supported and all three are real, not aspirational:

    claude   claude -p <prompt> --output-format json --add-dir <repo>
             --allowedTools "…" --model <id>              answer at .result
    codex    codex exec --ephemeral --sandbox read-only --skip-git-repo-check
             --model <id> --output-last-message <file> -   prompt on stdin,
                                                           answer in that file
    gemini   gemini -p <prompt> --output-format json --approval-mode default
             --model <id>                                  answer at .response

`--approval-mode default` is deliberate for gemini and is NOT `plan`: in a
headless run the default policy denies write_file, replace and run_shell_command
outright, while plan mode grants write access to markdown files in the working
directory. Default is the stricter of the two for a read-only measurement.

Every one of those commands additionally carries `childenv.isolation_flags`, which
is what stops the repository being measured from configuring the agent measuring
it. A committed `.claude/settings.json` supplies hooks and the CLI runs them: on
this machine, with the operator's real HOME and the provider credential in scope,
with no `--build` and no prompt. A CLI too old to offer those flags raises
`ProviderNotIsolated` and the lane stops rather than running the repository's
hooks and reporting a number.

A provider whose CLI is not installed raises ProviderUnavailable. It is not a
degradation: asking for codex and silently getting an unscored lane back reads
as "this repository had nothing to say", which is a different and much worse
claim than "codex is not installed here". Every OTHER failure mode (timeout,
non-zero exit, unparseable output) still returns `(None, note)` — a miner that
dies because one subprocess hiccupped is worse than one that reports zero
candidates and says why.

Auth is the subprocess's own business: each CLI reads its own key or logged-in
session from the environment childenv hands it. Nothing here accepts, logs, or
persists a key. Commands are always built as argv lists — never a shell string
— so a repo path or symbol name can't be read as shell syntax.
"""
from __future__ import annotations

import json
import re
import subprocess
import importlib.util
import sys
import tempfile
from pathlib import Path

# childenv lives next to the collectors; this module is executed with cwd=miners/, so add that
# directory to the path explicitly rather than relying on the caller's PYTHONPATH.
_COLLECTORS = Path(__file__).resolve().parents[1] / "collectors"
if str(_COLLECTORS) not in sys.path:
    sys.path.insert(0, str(_COLLECTORS))

import childenv  # noqa: E402


def _sibling(name: str):
    """Load a module from THIS skill's collectors directory, by path.

    measure-ext and measure-int both ship a `material_census`, and the collectors import each other
    by bare name. Any process that has put the other skill's directory on sys.path first -- the test
    suite does, and so does the mining orchestrator -- silently gets the wrong copy. That is how
    `from material_census import ProviderUnavailable` started failing: it resolved to the internal
    variant, which has no such symbol. Binding by file location cannot be shadowed.
    """
    spec = importlib.util.spec_from_file_location(f"_measure_ext_{name}", _COLLECTORS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_material_census = _sibling("material_census")
ProviderUnavailable = _material_census.ProviderUnavailable
resolve_cli = _material_census.resolve_cli

_HEAD_SHA_TIMEOUT = 30

# Where each provider's JSON envelope keeps the model's own text.
_ANSWER_KEY = {"claude": "result", "gemini": "response"}


def head_sha(repo: Path) -> str:
    """Current HEAD sha, or "" if this isn't a git repo / git isn't available."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=_HEAD_SHA_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def extract_json(text: str):
    """Pull a JSON array/object out of possibly-prose, possibly-fenced model text.

    Tries a direct parse, then a fenced ```json block, then the widest [..]/{..}
    span. Returns the parsed value, or None if nothing parses."""
    if not text:
        return None
    text = text.strip()
    for chunk in _json_chunks(text):
        try:
            return json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _json_chunks(text: str):
    yield text
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        yield fence.group(1).strip()
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if 0 <= start < end:
            yield text[start:end + 1]


def build_cmd(prompt: str, tools: list[str], model: str | None = None,
              add_dir: Path | None = None, provider: str = "claude",
              output_path: Path | None = None, executable: str | None = None,
              prompt_on_stdin: bool = False, isolation: list[str] | None = None,
              read_dirs: list[Path] | None = None) -> tuple[list[str], str | None]:
    """Build the fixed provider argv and optional stdin payload.

    `executable` is the absolute path resolve_cli() found. It defaults to the provider's bare
    name so a caller can inspect the shape of the command without a CLI installed.

    `isolation` is the argv from `childenv.isolation_flags` that stops the CLI honouring
    configuration committed to the repository it is pointed at. It is a parameter rather than
    something computed here so this function stays pure and a test can assert on the shape of
    the command without a CLI on the machine; `run_agent` is where the real one is resolved.

    `read_dirs` are further directories the agent may read -- in practice the pre-computed git
    history, which is what the withdrawn `Bash` grant used to fetch for itself.
    """
    exe = executable or provider
    extra = list(isolation or ())
    others = list(read_dirs or ())
    if provider == "claude":
        cmd = [exe, "-p", "--output-format", "json"]
        if not prompt_on_stdin:
            cmd.insert(2, prompt)
        if add_dir is not None:
            cmd += ["--add-dir", str(add_dir)]
        for extra_dir in others:
            cmd += ["--add-dir", str(extra_dir)]
        cmd += ["--allowedTools", " ".join(tools), *extra]
        if model:
            cmd += ["--model", model]
        return cmd, prompt if prompt_on_stdin else None
    if provider == "codex":
        # `read_dirs` is deliberately not translated to codex's `--add-dir`, which grants WRITE
        # access. `--sandbox read-only` already lets it read any path, and the prompt names the
        # directory.
        cmd = [
            exe, "exec", "--ephemeral", "--sandbox", "read-only",
            "--skip-git-repo-check", "--output-last-message", str(output_path), *extra,
        ]
        if model:
            cmd += ["--model", model]
        cmd.append("-")
        return cmd, prompt
    cmd = [exe, "-p", prompt, "--output-format", "json", "--approval-mode", "default", *extra]
    for extra_dir in others:
        cmd += ["--include-directories", str(extra_dir)]
    if model:
        cmd += ["--model", model]
    return cmd, None


def run_agent(prompt: str, cwd: Path, tools: list[str], timeout: int,
              model: str | None = None, add_dir: Path | None = None,
              provider: str = "claude",
              read_dirs: list[Path] | None = None) -> tuple[str | None, str]:
    """Run one headless provider turn. Degrades failures to a note; a missing CLI raises.

    A CLI that cannot be isolated from the measured tree's own configuration also raises. That
    is deliberate and it is not a degradation: continuing would mean running whatever the
    repository put in `.claude/settings.json` on this machine and calling the result a
    measurement.
    """
    exe, prompt_on_stdin, _note = resolve_cli(provider)   # raises ProviderUnavailable
    isolation = childenv.isolation_flags(provider, exe)   # raises ProviderNotIsolated
    output_path = None
    if provider == "codex":
        handle = tempfile.NamedTemporaryFile(prefix="repo-quality-codex-", suffix=".txt", delete=False)
        handle.close()
        output_path = Path(handle.name)
    cmd, input_text = build_cmd(
        prompt, tools, model=model, add_dir=add_dir, provider=provider, output_path=output_path,
        executable=exe, prompt_on_stdin=prompt_on_stdin, isolation=isolation,
        read_dirs=read_dirs,
    )
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), input=input_text, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL if input_text is None else None,
            # MODEL domain. The agent is pointed at the vendor's repository with Read/Grep/Glob/Bash,
            # so it is the one subprocess here with the broadest reach over their machine -- which is
            # exactly why it gets that provider's auth variables and nothing else. No AWS, GitHub,
            # database, SSH or registry credential is reachable from inside the agent's session.
            env=childenv.build_env(childenv.MODEL, provider=provider,
                                   passthrough=("HOME", "USERPROFILE", "XDG_CONFIG_HOME")),
        )
        text = output_path.read_text() if output_path and output_path.exists() else proc.stdout
    except FileNotFoundError:
        return None, f"{provider} CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"{provider} agent timed out after {timeout}s"
    except OSError as exc:
        return None, f"{provider} agent could not run: {exc}"
    finally:
        if output_path:
            output_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:300]
        return None, f"{provider} exited {proc.returncode}: {detail}"
    envelope = extract_json(text)
    answer_key = _ANSWER_KEY.get(provider)
    if answer_key and isinstance(envelope, dict) and isinstance(envelope.get(answer_key), str):
        return envelope[answer_key], f"{provider} agent completed"
    return text, f"{provider} agent completed"


def run_claude(prompt: str, cwd: Path, tools: list[str], timeout: int,
               model: str | None = None,
               add_dir: Path | None = None) -> tuple[str | None, str]:
    return run_agent(prompt, cwd, tools, timeout, model, add_dir, provider="claude")


ProviderNotIsolated = childenv.ProviderNotIsolated

__all__ = [
    "ProviderNotIsolated", "ProviderUnavailable", "build_cmd", "extract_json", "head_sha",
    "resolve_cli", "run_agent", "run_claude",
]
