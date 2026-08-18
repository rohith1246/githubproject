#!/usr/bin/env python3
"""childenv.py -- explicit child-process environments, one per trust domain.

Every subprocess that runs a model, or runs the repository's own code, gets an environment that
was BUILT here rather than inherited. The previous behaviour was `dict(os.environ)` everywhere,
which meant a repository's own `postinstall` hook or `setup.py` ran with `AWS_*`, `GITHUB_TOKEN`,
`ANTHROPIC_API_KEY`, database URLs and `SSH_AUTH_SOCK` in scope. That is not a theoretical
exposure: `--build` exists to run the repository's own install and test commands.

Three call sites are deliberately NOT routed through here and inherit the ambient environment:
`git_stats.run_git`, `git_history._git` and `agent_io.head_sha`. All three run `git` with
read-only subcommands and no repository-supplied argument reaches a shell. They are named rather
than glossed over, because "every subprocess" was the claim this module used to make and a reader
who greps for `subprocess.run(` finds them in a minute. Routing them through `run()` is the
obvious improvement and is not blocked by anything here.

An environment is only half of what confines a provider CLI. The other half is argv, because a
repository under measurement can ship agent configuration that the CLI will honour -- see
`isolation_flags()` below -- so the two live together here.

There is no library API here beyond four calls:

    env = build_env(STATIC)                     # deterministic readers: git, tree-sitter
    env = build_env(MODEL, passthrough=[...])   # a provider CLI, and nothing else
    env = build_env(BUILD, home=scratch)        # repository-controlled code
    proc = run(argv, domain=BUILD, cwd=..., timeout=...)
    argv += isolation_flags("claude", exe)      # ignore the measured tree's own config

`run()` also fixes the other half of the problem: `subprocess.run(timeout=...)` reaps only the
direct child, so a build that daemonises survives its own timeout. Children are started in a
new session and the whole process GROUP is signalled. `run(timeout=N)` returns within roughly
N + `POST_KILL_GRACE_SECONDS` whatever the child does with its descendants.

The four domains
----------------
STATIC  deterministic measurement. git, parsers, nothing that phones home.
MODEL   a provider CLI (claude / codex / gemini). Receives the provider's own auth variables
        BY NAME -- this module never reads their values, it copies the mapping -- plus nothing
        else. No cloud, no VCS, no database, no agent socket.
BUILD   repository-controlled code: dependency resolution, compilation, test execution.
        Receives NO credential of any kind, a scratch HOME, and CI=1.
GRADE   scoring a measurement document. No source tree, no credentials unless an optional
        provider call is enabled, in which case it is MODEL with an empty CWD.

Usage:
    from childenv import STATIC, MODEL, BUILD, GRADE, build_env, run, scratch_home

Environment variables:
    None are READ for their values. `build_env` copies a fixed set of non-secret variables
    (PATH, LANG, LC_ALL, TZ, TMPDIR, and platform equivalents) from the ambient environment,
    and for MODEL additionally copies the provider auth variables the caller names -- copying
    a mapping, never inspecting or logging a value. Nothing is written anywhere.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "STATIC", "MODEL", "BUILD", "GRADE", "DOMAINS",
    "CLAUDE_AUTH_VARS", "CODEX_AUTH_VARS", "GEMINI_AUTH_VARS", "provider_auth_vars",
    "build_env", "run", "scratch_home", "DeniedVariable", "POST_KILL_GRACE_SECONDS",
    "ProviderNotIsolated", "isolation_flags", "ISOLATION",
]

# How long `run()` waits for the child's pipes to reach EOF AFTER the process group has been
# killed. A descendant that left the group -- double fork plus `setsid()` -- still holds the
# inherited write ends, so EOF may never arrive and an unbounded read here makes `timeout=`
# bound nothing. Past this point the read ends are closed and whatever was buffered is the
# output that gets reported.
POST_KILL_GRACE_SECONDS = 2.0

STATIC = "static"
MODEL = "model"
BUILD = "build"
GRADE = "grade"
DOMAINS = (STATIC, MODEL, BUILD, GRADE)

# Non-secret variables every child needs to behave like a normal process on this machine. A
# child that cannot find `git` because PATH was stripped is not more secure, it is broken.
_BASE_PASSTHROUGH = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    # Windows needs these to start any process at all.
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)

# Variables that must NEVER reach a child in any domain. This is a tripwire, not the mechanism:
# the mechanism is that `build_env` starts from {} and adds. If one of these ever appears in a
# built environment, that is a bug in this module and `build_env` raises rather than returning it.
_NEVER = (
    "AWS_", "GITHUB_", "GH_", "GITLAB_", "DATABASE_", "STUDIO_", "MODAL_", "DOCKER_",
    "NPM_TOKEN", "NODE_AUTH_TOKEN", "PYPI_", "TWINE_", "CARGO_REGISTRY_TOKEN",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO",
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY",
    "VAULT_", "OKTA_", "SNOWFLAKE_", "AIRTABLE_",
)

# Provider auth is passed by NAME. This module never reads a value: `build_env` copies
# `os.environ[name]` into the child mapping without inspecting, logging or persisting it.
CLAUDE_AUTH_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
)
CODEX_AUTH_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME")
GEMINI_AUTH_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")

_PROVIDER_VARS = {
    "claude": CLAUDE_AUTH_VARS,
    "codex": CODEX_AUTH_VARS,
    "gemini": GEMINI_AUTH_VARS,
}


class DeniedVariable(RuntimeError):
    """A denied variable reached a built environment. Always a bug in this module."""


class ProviderNotIsolated(RuntimeError):
    """The installed provider CLI offers no way to ignore the measured tree's own configuration.

    Raised rather than warned about, because the alternative is running a repository's hooks on
    the operator's machine and calling it a measurement. The caller stops the run.
    """


# --- stopping the measured repository from configuring the agent that measures it ------------
#
# A repository can ship `.claude/settings.json`, and the provider CLI honours it: reproduced with
# no `--build` and no prompt, a `SessionStart` hook committed to a repository executed a shell
# command with the operator's real HOME and the provider credential live in its environment.
# `CLAUDE.md`, `.mcp.json`, project agents and project commands are the same problem one step
# quieter -- they steer the measurement instead of executing.
#
# The fix is a control the CLI enforces, not a promise, and it is checked against the installed
# binary rather than assumed: each flag below is used only if that binary's own `--help` lists it.
#
#   claude  --safe-mode               all customisations off -- CLAUDE.md, hooks, MCP servers,
#                                     plugins, skills, custom agents, commands -- from every
#                                     settings source. Auth, model and built-in tools unaffected.
#           --strict-mcp-config       no MCP server other than one we passed, and we pass none.
#           --no-session-persistence  no session transcript, so nothing the agent read is written
#                                     outside the output directory the operator was told about.
#   codex   --ignore-rules            no user or project execpolicy `.rules` file.
#           -c project_doc_max_bytes=0  no AGENTS.md from the tree. (Codex reads its config only
#                                     from CODEX_HOME, so the tree supplies nothing else, and the
#                                     callers already pass --ephemeral and --sandbox read-only.)
#   gemini  NOTHING. Checked against the gemini CLI source at v0.54.4, not assumed: its only
#           mechanism for ignoring a repository's own configuration is folder trust, which is
#           all-or-nothing and unreachable here -- an untrusted folder makes a headless `-p` run
#           refuse to start at all, and the two switches that exist (`--skip-trust`,
#           GEMINI_CLI_TRUST_WORKSPACE) move it the wrong way. In a trusted folder the repository's
#           `.gemini/settings.json` supplies hooks and MCP servers, its `GEMINI.md` is injected,
#           and its `.gemini/.env` can set GEMINI_SYSTEM_MD. There is no `--safe-mode`, no
#           `--setting-sources`, no way to disable context-file discovery from the command line,
#           and no `--no-session-persistence`. GEMINI_CLI_HOME relocates the CLI's own state and
#           does not touch any of that. So gemini is treated as un-isolatable and the model lanes
#           refuse to run under it rather than pretending otherwise.
#
# `required` is the subset without which the tree is still in charge. A CLI missing one of those
# raises: an old binary silently measuring a repository that configures its own measurement is
# exactly the failure this exists to stop.
ISOLATION: dict[str, dict] = {
    "claude": {
        "probe": ("--help",),
        "flags": (("--safe-mode", ("--safe-mode",)),
                  ("--strict-mcp-config", ("--strict-mcp-config",)),
                  ("--no-session-persistence", ("--no-session-persistence",))),
        "required": ("--safe-mode",),
    },
    "codex": {
        "probe": ("exec", "--help"),
        "flags": (("--ignore-rules", ("--ignore-rules",)),
                  ("--config", ("-c", "project_doc_max_bytes=0"))),
        "required": ("--ignore-rules", "--config"),
    },
    "gemini": {"probe": (), "flags": (), "required": ("--there-is-no-such-flag",)},
}

_HELP_TIMEOUT = 30
_help_cache: dict[str, str] = {}


def _help_text(executable: str, probe: tuple[str, ...]) -> str:
    """The CLI's own `--help`, once per executable per process. "" if it cannot be read."""
    if executable in _help_cache:
        return _help_cache[executable]
    try:
        proc = subprocess.run([executable, *probe], capture_output=True, text=True,
                              errors="replace", timeout=_HELP_TIMEOUT,
                              stdin=subprocess.DEVNULL, env=build_env(STATIC))
        text = (proc.stdout or "") + (proc.stderr or "")
    except (subprocess.SubprocessError, OSError):
        text = ""
    _help_cache[executable] = text
    return text


def isolation_flags(provider: str, executable: str) -> list[str]:
    """Argv that stops `provider`'s CLI honouring configuration supplied by the measured tree.

    provider     claude / codex / gemini.
    executable   the resolved absolute path of the CLI, so the flags match the binary that will
                 actually run rather than whichever one is on PATH at some later moment.

    Raises ProviderNotIsolated when the installed CLI does not advertise the flags that make the
    isolation real.
    """
    spec = ISOLATION.get(provider)
    if spec is None:
        raise ProviderNotIsolated(f"unknown provider {provider!r}")
    help_text = _help_text(executable, spec["probe"]) if spec["probe"] else ""
    missing = [name for name in spec["required"] if name not in help_text]
    if missing:
        raise ProviderNotIsolated(
            f"the installed `{provider}` CLI does not offer {', '.join(missing)}, so a "
            f"`.claude/settings.json`, `CLAUDE.md`, `AGENTS.md` or MCP declaration committed to "
            f"the repository being measured would configure the agent measuring it -- hooks "
            f"included, which is arbitrary code execution on this machine. This run stops here. "
            f"Upgrade the CLI, or choose a --provider whose CLI supports being isolated."
        )
    argv: list[str] = []
    for name, flag in spec["flags"]:
        if name in help_text:
            argv.extend(flag)
    return argv


def _system_path(path_value: str) -> str:
    """Drop every PATH entry that lives under the operator's home directory.

    Keeps ordering for the entries that survive. Falls back to a minimal system PATH if that would
    leave nothing, because a child with no PATH cannot find `git` and fails for the wrong reason.
    """
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        return path_value

    kept = []
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except (OSError, ValueError):
            continue
        if resolved == home or home in resolved.parents:
            continue
        kept.append(entry)
    return os.pathsep.join(kept) if kept else os.pathsep.join(
        ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"))


def provider_auth_vars(provider: str) -> tuple[str, ...]:
    """The auth variable NAMES one provider CLI is allowed to see. Unknown provider -> ()."""
    return _PROVIDER_VARS.get(provider, ())


def _assert_clean(env: dict[str, str]) -> dict[str, str]:
    for key in env:
        for denied in _NEVER:
            if key.startswith(denied) or key == denied:
                raise DeniedVariable(
                    f"{key!r} was about to be handed to a child process. build_env() starts from "
                    f"an empty mapping and adds only what a domain declares, so this can only "
                    f"mean a caller passed it explicitly. Refusing."
                )
    return env


@contextmanager
def scratch_home(prefix: str = "repo-quality-home-"):
    """A throwaway HOME for one child process.

    Build and test commands read `~/.npmrc`, `~/.pypirc`, `~/.gitconfig`, `~/.docker/config.json`
    and `~/.aws/credentials`. Pointing HOME at an empty directory removes every one of those in a
    single move, and does it without needing to know which files a given ecosystem will look for.
    """
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield Path(tmp)


def build_env(
    domain: str,
    *,
    home: Path | str | None = None,
    provider: str | None = None,
    passthrough: tuple[str, ...] | list[str] = (),
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment for one trust domain, starting from nothing.

    domain       one of STATIC / MODEL / BUILD / GRADE.
    home         HOME (and USERPROFILE) for the child. Required for BUILD -- pass a
                 `scratch_home()` directory so the child cannot read the operator's credentials.
    provider     for MODEL: which provider's auth variable NAMES to copy through.
    passthrough  extra ambient variable names to copy, if present. Denied names are rejected.
    extra        literal key/value pairs to set. Denied names are rejected.
    """
    if domain not in DOMAINS:
        raise ValueError(f"unknown trust domain {domain!r}; expected one of {DOMAINS}")

    env: dict[str, str] = {}
    for name in (*_BASE_PASSTHROUGH, *passthrough):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    # PATH is the one "harmless" variable that is not harmless for untrusted code. A developer's
    # PATH routinely contains ~/.local/bin, ~/.cargo/bin, tool shims and plugin directories, so
    # handing it to a repository's own postinstall hook tells that hook the operator's username and
    # points it at binaries inside their home. Scrubbing HOME and then leaving those entries on PATH
    # protects nothing.
    if domain == BUILD and env.get("PATH"):
        env["PATH"] = _system_path(env["PATH"])

    # TMPDIR is deliberately NOT inherited for BUILD: a shared temp directory is a channel
    # between the untrusted child and everything else on the box.
    if domain == BUILD:
        if home is None:
            raise ValueError("BUILD requires an explicit scratch home; see scratch_home()")
        env["TMPDIR"] = str(Path(home) / "tmp")
        Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
        # Most ecosystems take a non-interactive, no-colour, no-telemetry path when CI is set,
        # which is both faster and less likely to prompt and hang.
        env.update({
            "CI": "1",
            "DEBIAN_FRONTEND": "noninteractive",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NO_COLOR": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "DO_NOT_TRACK": "1",
        })
    elif os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]

    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)

    if domain == MODEL and provider:
        for name in provider_auth_vars(provider):
            value = os.environ.get(name)
            if value is not None:
                env[name] = value

    if extra:
        env.update(extra)

    return _assert_clean(env)


def run(
    argv: list[str],
    *,
    domain: str,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    input_text: str | None = None,
    capture_output: bool = True,
    home: Path | str | None = None,
    provider: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a fixed-argv child in a trust domain, and kill its whole process group on timeout.

    `subprocess.run(timeout=...)` sends SIGKILL to the direct child only. A build that forks a
    daemon, or a test runner that leaves a server behind, therefore outlives its own timeout and
    keeps holding ports, CPU and the repository checkout. Starting the child in its own session
    and signalling the group closes that.

    On timeout this raises `subprocess.TimeoutExpired` carrying whatever output was captured,
    and it does so within roughly `timeout` + `POST_KILL_GRACE_SECONDS`: the post-kill read is
    bounded, so a descendant that escaped the group cannot hold the call open on its copy of
    the pipes.
    """
    child_env = env if env is not None else build_env(domain, home=home, provider=provider)
    popen_kwargs: dict = {
        "cwd": None if cwd is None else str(cwd),
        "env": child_env,
        "text": True,
        "stdout": subprocess.PIPE if capture_output else None,
        "stderr": subprocess.PIPE if capture_output else None,
        "stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = subprocess.Popen(argv, **popen_kwargs)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = _drain_after_kill(proc)
        raise subprocess.TimeoutExpired(argv, timeout or 0, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _as_text(value) -> str | None:
    """`TimeoutExpired.output` carries the raw bytes even for a text-mode child."""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _drain_after_kill(proc: subprocess.Popen) -> tuple[str | None, str | None]:
    """Collect the killed child's output without ever waiting on EOF forever.

    The direct child is dead by the time this runs, so in the ordinary case the pipes are
    already at EOF and this returns at once. When a descendant escaped the group it still holds
    the write ends open, and no amount of waiting will close them -- so the wait is bounded and
    the read ends are closed by hand.
    """
    try:
        return proc.communicate(timeout=POST_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired as expired:
        out, err = _as_text(expired.output), _as_text(expired.stderr)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        proc.wait(timeout=POST_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    return out, err


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate, then kill, the child's entire process group. Never raises."""
    if os.name != "posix":
        proc.kill()
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


if __name__ == "__main__":
    # Self-test: prove the denied set never survives into a built environment, and that each
    # provider sees only its own auth. Canaries are set unconditionally so the test is
    # meaningful on a machine that already has real credentials exported.
    os.environ["AWS_SECRET_ACCESS_KEY"] = "canary-aws"
    os.environ["GITHUB_TOKEN"] = "canary-gh"
    os.environ["SSH_AUTH_SOCK"] = "/tmp/canary.sock"
    os.environ["ANTHROPIC_API_KEY"] = "canary-anthropic"
    os.environ["OPENAI_API_KEY"] = "canary-openai"

    def _no_denied(built: dict, label: str) -> None:
        leaked = [k for k in built if k.startswith(("AWS_", "GITHUB_")) or k == "SSH_AUTH_SOCK"]
        assert not leaked, f"{label}: {leaked}"

    with scratch_home() as h:
        for dom in (STATIC, GRADE):
            built = build_env(dom)
            _no_denied(built, dom)
            assert "ANTHROPIC_API_KEY" not in built, dom
            assert "OPENAI_API_KEY" not in built, dom

        b = build_env(BUILD, home=h)
        _no_denied(b, "build")
        assert "ANTHROPIC_API_KEY" not in b, "build must never see provider auth"
        assert "OPENAI_API_KEY" not in b, "build must never see provider auth"
        assert b["HOME"] == str(h) and b["CI"] == "1"
        assert b["TMPDIR"].startswith(str(h)), "build must not share the host temp directory"
        home_str = str(Path.home())
        assert home_str not in b.get("PATH", ""), (
            "build PATH still points into the operator's home: " + b.get("PATH", ""))
        assert b.get("PATH"), "build still needs a usable PATH"
        # every other domain keeps the operator's PATH -- they run OUR code, not the repository's
        assert home_str in build_env(STATIC).get("PATH", home_str)

        m = build_env(MODEL, home=h, provider="claude")
        _no_denied(m, "model")
        assert m["ANTHROPIC_API_KEY"] == "canary-anthropic", "model needs its own provider auth"
        assert "OPENAI_API_KEY" not in m, "claude must not receive another provider's auth"

        c = build_env(MODEL, home=h, provider="codex")
        assert c["OPENAI_API_KEY"] == "canary-openai"
        assert "ANTHROPIC_API_KEY" not in c, "codex must not receive another provider's auth"

        try:
            build_env(STATIC, extra={"AWS_SECRET_ACCESS_KEY": "x"})
        except DeniedVariable:
            pass
        else:  # pragma: no cover - the tripwire must fire
            raise AssertionError("extra= must not be able to smuggle a denied variable through")

    print("childenv self-test passed", file=sys.stderr)
