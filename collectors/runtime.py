#!/usr/bin/env python3
"""runtime.py -- which language runtime the repository asks for, and which one this host can give it.

Usage (library module, no CLI):

    import runtime
    plan = runtime.resolve(project_root, env, allow_home=False)

    project_root  Path to the directory whose declarations are read. Read-only: nothing here
                  writes, installs, or executes anything the repository supplied.
    env           The child environment the project's commands will run in. Used to find
                  candidates on PATH; never mutated. `plan.overlay` is what the caller applies.
    allow_home    Whether a runtime installed under the operator's HOME may be selected. FALSE is
                  the default and is the ONLY safe answer on a machine we do not own -- see the
                  asymmetry note below. measure-int passes True; measure-ext never does.

Returns a `Plan`:

Every `note` written here is EMITTED PROSE and has to survive measure-ext's sentence rules, which
reject a quoted string. Two possessive apostrophes in one sentence look exactly like one, so the
notes say "belongs to the runner" rather than "is the runner's" -- the alternative is a note that
is silently replaced by an empty string at the boundary, which is the worst of the three outcomes
because a null with no reason beside it is the thing a reader downgrades a repository over.

    plan.records    one record per lane the tree declares: what was asked for, what was chosen,
                    whether the ask was satisfiable here, and why not when it was not.
    plan.overlay    environment variables to apply to that project's commands (a PATH prefix, a
                    JAVA_HOME). Empty when nothing needed changing.
    plan.interpreter  {lane: absolute executable} for lanes where the caller invokes the runtime
                    by path rather than by name -- python is the only one today.
    plan.unsatisfied  the lanes the tree asked for and this host cannot supply.

Environment variables: this module READS none for its own configuration. It inspects the PATH of
the environment it is HANDED, and it copies no value anywhere.

WHY THIS EXISTS. `wrong_runtime` was 27 of the 417-repo corpus: repositories that failed because
we ran the wrong interpreter, not because anything was wrong with them. A repository pinned to
Python 3.8 installed under 3.14 fails with `configparser.SafeConfigParser` gone and pip refusing a
2019 wheel's metadata; a package.json with `engines.node: 16` installed under Node 22 fails with
`ERR_OSSL_EVP_UNSUPPORTED`; a pom.xml with `maven.compiler.source 7` fails on JDK 17 with "Source
option 7 is no longer supported". Every one of those was recorded as the repository's fault. None
of them were.

TWO RULES DECIDE EVERYTHING HERE.

  * **What the repository declares is what we run.** Not the newest runtime, and not the one that
    happens to be first on PATH. A project states a minimum because that is what it was built and
    tested against, so the candidate nearest the declared floor is the one most likely to work.
    Newer is not safer; on this corpus newer is the failure.

  * **A runtime this host does not have is OUR limit, never the repository's.** When the ask
    cannot be met the record says so and the caller attributes every downstream failure to the
    `runner`. That distinction is load-bearing everywhere else in this codebase and it is the
    whole reason a version mismatch must be named rather than absorbed into a build verdict.

THE ASYMMETRY, and why `allow_home` exists. Version managers -- nvm, pyenv, rustup, asdf, mise,
uv, volta, rbenv, sdkman -- install under the operator's home directory, and `childenv` strips
home-directory entries from the BUILD PATH on purpose: handing a repository's own postinstall hook
a path inside someone's home tells it their username and points it at their binaries. On OUR
disposable container that costs nothing and buys most of the available runtime coverage. On a
vendor's laptop it is a change to the trust boundary nobody agreed to. So the search finds those
toolchains either way, tags each candidate with whether it lives under HOME, and the CALLER
decides. measure-ext selects only from the system; measure-int selects from everything present.

Nothing here installs a runtime. Selecting among what is already on the machine is a read; putting
a new toolchain on it is a mutation, and that decision belongs to the operator, not to a probe.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The lanes a declaration can name. Fixed vocabulary: the emitted record reports lanes by name and
# an open-ended set there would be an undeclared field by another route.
LANES = ("node", "python", "go", "rust", "ruby", "java", "dotnet")

# How long any single version probe may take. A runtime that cannot say its own version in this
# long is not a runtime we are going to build with, and an unbounded probe here would put an
# unaccounted subprocess inside a budget that is supposed to be one allowance.
_PROBE_TIMEOUT = 15
# The whole search is bounded by how many probes it may spend, because a host with forty JDKs
# should not cost forty subprocesses per project root.
_MAX_PROBES = 24

# `.nvmrc` may name an LTS codename instead of a number. Recognised because the alternative is
# reading "lts/hydrogen" as "no constraint" and then running Node 24 against a Node 18 project.
_NODE_CODENAMES = {
    "argon": 4, "boron": 6, "carbon": 8, "dubnium": 10, "erbium": 12, "fermium": 14,
    "gallium": 16, "hydrogen": 18, "iron": 20, "jod": 22, "krypton": 24,
}

_VERSION_IN_PATH = re.compile(r"(?:^|[/\\@-])v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[/\\-]|$)")


# --- versions and the specs that constrain them ----------------------------------------------

def parse_version(text: str | None) -> tuple[int, ...] | None:
    """The leading numeric version in `text`, as a tuple. None when there is not one.

    Tolerant on purpose: `v18.17.0`, `18`, `1.8.0_382`, `openjdk 21.0.2` and `go1.22.12` all
    appear in the files and command output this module reads.
    """
    if not text:
        return None
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text.strip())
    if match is None:
        return None
    return tuple(int(g) for g in match.groups() if g is not None)


def _norm_java(version: tuple[int, ...]) -> tuple[int, ...]:
    """Java's two numbering schemes as one. `1.8` and `8` are the same JDK; `1.5` is release 5."""
    if version and version[0] == 1 and len(version) > 1:
        return version[1:]
    return version


@dataclass(frozen=True)
class Interval:
    """A half-open version window: `low` inclusive, `high` exclusive. None means unbounded."""

    low: tuple[int, ...] | None = None
    high: tuple[int, ...] | None = None

    def contains(self, version: tuple[int, ...]) -> bool:
        if self.low is not None and version[:len(self.low)] < self.low:
            return False
        if self.high is not None and version[:len(self.high)] >= self.high:
            return False
        return True


def _bump(version: tuple[int, ...], place: int) -> tuple[int, ...]:
    """The exclusive upper bound one step above `version` at `place`. `_bump((18,17),0)` -> (19,)."""
    padded = list(version[:place + 1]) + [0] * (place + 1 - len(version))
    padded[place] += 1
    return tuple(padded[:place + 1])


# How much of a bare pinned version actually constrains compatibility, per lane. `.python-version`
# 3.9.7 must not exclude the 3.9.18 this host ships -- the boundary in Python is the minor, in Node
# it is the major. Getting this wrong turns a satisfiable ask into a reported mismatch.
_PIN_PRECISION = {"python": 2, "ruby": 2, "go": 2, "rust": 2,
                  "node": 1, "java": 1, "dotnet": 1}

_HAS_OPERATOR = re.compile(r"[><=^~*|]|\bx\b|\.x")


def parse_spec(spec: str, lane: str, floor: bool = False) -> list[Interval]:
    """The version windows a declaration allows. `[]` means "declared, but nothing constrains it".

    Handles the four shapes that actually appear: a bare version or pin (`18`, `3.9.7`, `v20.11`),
    an npm-style range (`^18.0.0`, `~3.9`, `>=14 <21`, `18.x`, `16 || 18`), a PEP 440 requires-python
    (`>=3.8,<3.13`, `~=3.11`), and Java's release levels (`1.8`, `11`, `17`).

    `floor` says a bare version is a MINIMUM rather than a pin, which is what `go.mod`'s go
    directive and `Cargo.toml`'s rust-version mean and what a version file does not.
    """
    text = (spec or "").strip().strip("\"'")
    if not text or text in ("*", "x", "latest", "current", "node", "system", "stable", "default"):
        return []
    if lane == "node":
        codename = _NODE_CODENAMES.get(text.lower().replace("lts/", "").strip())
        if codename is not None:
            return [Interval((codename,), _bump((codename,), 0))]
        if text.lower().startswith("lts"):
            return []
    if lane == "java":
        # A release level is a floor and, for the levels a modern JDK has dropped, also a ceiling.
        # `javac --release 7` does not exist past JDK 19 and `--release 8` is gone past JDK 25, so
        # a project asking for 7 on JDK 21 fails for a reason that is ours to avoid, not theirs.
        level = _norm_java(parse_version(text) or ())
        if not level:
            return []
        floor = (level[0],)
        if level[0] <= 7:
            return [Interval(floor, (20,))]
        if level[0] == 8:
            return [Interval(floor, (25,))]
        return [Interval(floor)]

    if not _HAS_OPERATOR.search(text):
        # A bare version. Either a minimum the tree states it needs, or a pin -- and a pin binds
        # only as far as the lane's compatibility boundary goes.
        version = parse_version(text)
        if version is None:
            return []
        # On a 0.x release the minor IS the compatibility boundary in every ecosystem -- semver
        # says so explicitly and Node 0.10 against Node 0.12 is the reason it does.
        precision = _PIN_PRECISION.get(lane, 2)
        if version[0] == 0:
            precision = max(precision, 2)
        if floor:
            return [Interval(version[:precision])]
        base = version[:precision]
        return [Interval(base, _bump(base, len(base) - 1))]

    # `||` is an npm union and `,` a PEP 440 intersection. Split unions first; within a branch,
    # every clause narrows the same window.
    windows: list[Interval] = []
    for branch in re.split(r"\|\|", text):
        low: tuple[int, ...] | None = None
        high: tuple[int, ...] | None = None
        matched = False
        for op, raw in re.findall(r"(>=|<=|==|!=|~=|\^|~|>|<|=)?\s*v?([0-9][0-9.]*[0-9x*]?|\d)",
                                  branch):
            wildcard = raw.endswith(("x", "*"))
            version = parse_version(raw)
            if version is None:
                continue
            matched = True
            place = max(0, len(version) - 1)
            # `=` is a PIN, not a floor. Grouping it with `>=` turned npm's `=18.0.0` into
            # "18 and newer", which both selects a runtime the tree did not ask for and hides the
            # mismatch it was written to catch. Falling through to the bare-version branch below
            # gives it the same single-release window `==` gets.
            if op == ">=":
                low = version if low is None else max(low, version)
            elif op == ">":
                low = _bump(version, place) if low is None else max(low, _bump(version, place))
            elif op == "<=":
                bound = _bump(version, place)
                high = bound if high is None else min(high, bound)
            elif op == "<":
                high = version if high is None else min(high, version)
            elif op == "^":
                # Caret on a 0.x release pins the minor, which is semver's own rule.
                place = 1 if version[0] == 0 and len(version) > 1 else 0
                low, high = version, _bump(version, place)
            elif op in ("~", "~="):
                place = min(1, max(0, len(version) - 1))
                low, high = version, _bump(version, place)
            elif op == "!=":
                continue
            elif wildcard:
                low, high = version, _bump(version, max(0, len(version) - 1))
            else:
                # A bare version. `.nvmrc` and `.python-version` mean it as a pin; a manifest
                # range means the exact release. Either way the window is that release line.
                low, high = version, _bump(version, place)
        if matched:
            windows.append(Interval(low, high))
    return windows


def satisfies(version: tuple[int, ...], windows: list[Interval]) -> bool:
    """Does `version` fall in any window? An empty window list constrains nothing."""
    return not windows or any(w.contains(version) for w in windows)


def _display(windows: list[Interval], spec: str) -> str:
    """The ask, as a token-safe string. `emit_allowlist.VERSION` rejects `<` and `>`."""
    if not windows:
        return re.sub(r"[^A-Za-z0-9._@+-]", "", spec)[:40] or "any"
    low, high = windows[0].low, windows[-1].high
    if low is not None and high is not None and high == _bump(low, len(low) - 1):
        # The window is exactly one release line, so naming both ends says the same thing twice.
        return ".".join(str(n) for n in low)
    text = ".".join(str(n) for n in low) if low else ""
    top = ".".join(str(n) for n in high) if high else ""
    if text and top:
        return f"{text}-{top}"
    return text or (f"to-{top}" if top else "any")


# --- what the tree declares -------------------------------------------------------------------

def _text(path: Path, cap: int = 200_000) -> str:
    try:
        return path.read_text(errors="replace")[:cap]
    except OSError:
        return ""


def _first_line(path: Path) -> str | None:
    """The first non-empty, non-comment line of a version file. `.nvmrc` and friends hold one."""
    for line in _text(path, 400).splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            return stripped
    return None


def _first_group(pattern: str, body: str) -> str | None:
    """The first capture of `pattern` in `body`, or None.

    MULTILINE matters here and its absence was a bug: `^\\s*go\\s+([0-9.]+)` without it anchors at
    the start of the FILE, so a go.mod that begins `module example.com/x` -- which is every go.mod
    ever written -- declared no Go version at all. The same applied to requires-python, poetry's
    python, rust-version and Gemfile's ruby.
    """
    match = re.search(pattern, body, re.I | re.M)
    return match.group(1).strip() if match else None


def _json_at(path: Path, *keys: str):
    """A nested value out of a JSON file, or None. Never raises on a malformed file."""
    try:
        node = json.loads(_text(path))
    except (ValueError, TypeError):
        return None
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _tool_versions(root: Path) -> list[tuple[str, str]]:
    """`.tool-versions` (asdf and mise), as (lane, spec) pairs for the lanes we know."""
    aliases = {"nodejs": "node", "node": "node", "python": "python", "golang": "go", "go": "go",
               "rust": "rust", "ruby": "ruby", "java": "java", "dotnet": "dotnet",
               "dotnet-core": "dotnet"}
    out: list[tuple[str, str]] = []
    for line in _text(root / ".tool-versions", 20_000).splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) < 2:
            continue
        lane = aliases.get(parts[0].lower())
        if lane is None:
            continue
        # `java temurin-17.0.9` and `python 3.11.7 3.10.13` both occur; take the first version.
        spec = re.sub(r"^[a-z-]+-", "", parts[1], flags=re.I)
        out.append((lane, spec))
    return out


def declarations(root: Path) -> list[dict]:
    """Every runtime version this tree asks for. `[{"lane","spec","source"}]`, most specific first.

    Read-only, and it never guesses: a lane appears here only because a file in the tree names a
    version for it. What a repository does not declare is not a constraint we get to invent.
    """
    found: list[dict] = []

    def add(lane: str, spec, source: str, floor: bool = False) -> None:
        if isinstance(spec, (int, float)):
            spec = str(spec)
        if isinstance(spec, str) and spec.strip():
            found.append({"lane": lane, "spec": spec.strip()[:60], "source": source,
                          "floor": floor})

    # A pinned version file is the most specific statement a tree can make, so it comes first and
    # `resolve` honours the first record for a lane.
    add("node", _first_line(root / ".nvmrc"), ".nvmrc")
    add("node", _first_line(root / ".node-version"), ".node-version")
    add("node", _json_at(root / "package.json", "engines", "node"), "package.json engines")
    add("node", _json_at(root / "package.json", "volta", "node"), "package.json volta")

    add("python", _first_line(root / ".python-version"), ".python-version")
    pyproject = _text(root / "pyproject.toml")
    add("python", _first_group(r"^\s*requires-python\s*=\s*[\"']([^\"']+)", pyproject),
        "pyproject requires-python")
    add("python", _first_group(r"^\s*python\s*=\s*[\"']([^\"']+)", pyproject),
        "pyproject poetry python")
    add("python", _first_group(r"python_requires\s*=\s*[\"']([^\"']+)", _text(root / "setup.py")),
        "setup.py python_requires")
    add("python", _first_group(r"^\s*python_requires\s*=\s*(.+)$", _text(root / "setup.cfg")),
        "setup.cfg python_requires")
    add("python", _first_group(r"python-([0-9.]+)", _text(root / "runtime.txt", 200)),
        "runtime.txt")

    # `go 1.21` in a go.mod is the language version the module NEEDS, not the toolchain it wants,
    # and the same is true of Cargo's rust-version. Both are floors; a version file is not.
    add("go", _first_group(r"^\s*go\s+([0-9][0-9.]*)", _text(root / "go.mod")), "go.mod go",
        floor=True)

    toolchain = _text(root / "rust-toolchain.toml") or _text(root / "rust-toolchain")
    add("rust", _first_group(r"channel\s*=\s*[\"']([^\"']+)", toolchain) or
        (toolchain.strip().splitlines()[0].strip() if toolchain.strip() and
         "=" not in toolchain and "[" not in toolchain else None), "rust-toolchain")
    add("rust", _first_group(r"^\s*rust-version\s*=\s*[\"']([^\"']+)", _text(root / "Cargo.toml")),
        "Cargo.toml rust-version", floor=True)

    add("ruby", _first_line(root / ".ruby-version"), ".ruby-version")
    add("ruby", _first_group(r"^\s*ruby\s+[\"']([^\"']+)", _text(root / "Gemfile")), "Gemfile ruby")

    pom = _text(root / "pom.xml")
    add("java", _first_group(r"<maven\.compiler\.release>([^<]+)<", pom), "pom.xml compiler release")
    add("java", _first_group(r"<maven\.compiler\.source>([^<]+)<", pom), "pom.xml compiler source")
    add("java", _first_group(r"<java\.version>([^<]+)<", pom), "pom.xml java version")
    add("java", _first_group(r"<release>([^<]+)</release>", pom), "pom.xml release")
    add("java", _first_group(r"<source>([^<]+)</source>", pom), "pom.xml source")
    gradle = (_text(root / "build.gradle") + "\n" + _text(root / "build.gradle.kts") + "\n"
              + _text(root / "gradle.properties", 20_000))
    add("java", _first_group(r"JavaLanguageVersion\.of\((\d+)", gradle), "gradle toolchain")
    add("java", _first_group(r"(?:source|target)Compatibility\s*=?\s*[\"']?"
                             r"(?:JavaVersion\.VERSION_)?([0-9_.]+)", gradle),
        "gradle compatibility")

    add("dotnet", _json_at(root / "global.json", "sdk", "version"), "global.json sdk")

    for lane, spec in _tool_versions(root):
        add(lane, spec, ".tool-versions")

    # First record per lane wins, and the order above is the precedence: a pinned version file
    # beats a manifest range, which beats `.tool-versions`.
    seen: set[str] = set()
    ordered: list[dict] = []
    for record in found:
        if record["lane"] in seen:
            continue
        seen.add(record["lane"])
        record["windows"] = parse_spec(record["spec"], record["lane"], record["floor"])
        record["requested"] = _display(record["windows"], record["spec"])
        ordered.append(record)
    return ordered


# --- what this host has -----------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One runtime installation this host actually carries."""

    lane: str
    version: tuple[int, ...]
    bin_dir: Path
    executable: Path
    under_home: bool
    home_dir: Path | None = None      # java only: what JAVA_HOME must be set to


def _home() -> Path | None:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def _under_home(path: Path) -> bool:
    home = _home()
    if home is None:
        return False
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    return resolved == home or home in resolved.parents


def _path_dirs(env: dict) -> list[Path]:
    out: list[Path] = []
    for entry in (env.get("PATH") or "").split(os.pathsep):
        if entry:
            out.append(Path(entry))
    return out


# Where version managers and distributions put runtimes. Globbed, not executed, and every hit is
# tagged with whether it sits under the operator's home so the caller can refuse it.
_SEARCH_GLOBS: dict[str, tuple[str, ...]] = {
    "node": ("~/.nvm/versions/node/*/bin", "~/.volta/tools/image/node/*/bin",
             "~/.asdf/installs/nodejs/*/bin", "~/.local/share/mise/installs/node/*/bin",
             "~/.local/share/mise/installs/nodejs/*/bin", "~/n/versions/node/*/bin",
             "~/.fnm/node-versions/*/installation/bin", "/opt/homebrew/opt/node@*/bin",
             "/usr/local/opt/node@*/bin", "/usr/local/lib/nodejs/*/bin", "/opt/node-*/bin"),
    "python": ("~/.pyenv/versions/*/bin", "~/.asdf/installs/python/*/bin",
               "~/.local/share/mise/installs/python/*/bin",
               "~/.local/share/uv/python/*/bin", "/opt/homebrew/opt/python@*/bin",
               "/usr/local/opt/python@*/bin", "/opt/python/*/bin",
               "/usr/local/lib/python*/bin"),
    "go": ("/usr/local/go/bin", "/usr/lib/go-*/bin", "~/.asdf/installs/golang/*/go/bin",
           "~/.local/share/mise/installs/go/*/bin", "/opt/go/bin", "/opt/homebrew/opt/go@*/bin"),
    "ruby": ("~/.rbenv/versions/*/bin", "~/.asdf/installs/ruby/*/bin",
             "~/.local/share/mise/installs/ruby/*/bin", "~/.rvm/rubies/*/bin",
             "/opt/homebrew/opt/ruby@*/bin", "/usr/local/opt/ruby@*/bin"),
    "rust": ("~/.rustup/toolchains/*/bin", "~/.cargo/bin"),
    "java": ("/usr/lib/jvm/*", "/Library/Java/JavaVirtualMachines/*/Contents/Home",
             "~/Library/Java/JavaVirtualMachines/*/Contents/Home", "~/.sdkman/candidates/java/*",
             "/opt/homebrew/opt/openjdk@*/libexec/openjdk.jdk/Contents/Home",
             "/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home",
             "/usr/local/opt/openjdk@*/libexec/openjdk.jdk/Contents/Home",
             "~/.asdf/installs/java/*", "~/.local/share/mise/installs/java/*", "/opt/java/*"),
    "dotnet": ("/usr/share/dotnet", "/usr/local/share/dotnet", "~/.dotnet",
               "/opt/homebrew/opt/dotnet/libexec"),
}

_EXE = {"node": "node", "python": "python3", "go": "go", "ruby": "ruby", "rust": "rustc",
        "java": "javac", "dotnet": "dotnet"}

_VERSION_ARG = {"node": ("--version",), "python": ("--version",), "go": ("version",),
                "ruby": ("--version",), "rust": ("--version",), "java": ("-version",),
                "dotnet": ("--version",)}


def _expand(pattern: str) -> list[Path]:
    """One search glob to the directories it matches. `~` is expanded; nothing is executed."""
    text = os.path.expanduser(pattern)
    if "*" not in text:
        path = Path(text)
        return [path] if path.is_dir() else []
    # Split on the first wildcard so `Path.glob` gets a concrete anchor.
    head, _, tail = text.partition("*")
    anchor = Path(head).parent if not head.endswith(os.sep) else Path(head)
    try:
        return sorted(p for p in anchor.glob(Path(text).relative_to(anchor).as_posix())
                      if p.is_dir())
    except (OSError, ValueError):
        return []


def _probe_version(executable: Path, lane: str, env: dict, budget: list[int]
                   ) -> tuple[int, ...] | None:
    """Ask a runtime its own version. Bounded, and it spends from the shared probe allowance."""
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    try:
        proc = subprocess.run([str(executable), *_VERSION_ARG[lane]], capture_output=True,
                              text=True, errors="replace", timeout=_PROBE_TIMEOUT,
                              stdin=subprocess.DEVNULL, env=dict(env))
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "") + (proc.stderr or "")
    version = parse_version(text.replace("go", " ").replace("Python", " "))
    return _norm_java(version) if lane == "java" and version else version


def _java_release_file(home: Path) -> tuple[int, ...] | None:
    """A JDK's version out of its own `release` file, so a JDK costs no subprocess to identify."""
    body = _text(home / "release", 8_000)
    raw = _first_group(r"JAVA_VERSION=\"?([0-9._]+)", body)
    return _norm_java(parse_version(raw) or ()) or None


def candidates(lane: str, env: dict, budget: list[int]) -> list[Candidate]:
    """Every installation of `lane` this host carries, newest first, each tagged under_home.

    Versions come from the path where the path states them (`node/v18.17.0/bin`, `python3.11`,
    a JDK's `release` file) and from a bounded `--version` probe where it does not.
    """
    found: dict[Path, Candidate] = {}
    exe_name = _EXE[lane]

    def consider(bin_dir: Path, executable: Path, home_dir: Path | None = None) -> None:
        if executable in found or not executable.is_file():
            return
        version: tuple[int, ...] | None = None
        if lane == "java" and home_dir is not None:
            version = _java_release_file(home_dir)
        if version is None and lane == "python":
            # `python3.11` states its version in its own name, which is free to read.
            named = re.fullmatch(r"python(\d+)\.(\d+)", executable.name)
            if named:
                version = (int(named.group(1)), int(named.group(2)))
        if version is None:
            in_path = _VERSION_IN_PATH.search(str(bin_dir))
            if in_path:
                version = tuple(int(g) for g in in_path.groups() if g is not None)
        if version is None:
            version = _probe_version(executable, lane, env, budget)
        if version:
            found[executable] = Candidate(lane, version, bin_dir, executable,
                                          _under_home(executable), home_dir)

    search_dirs = list(_path_dirs(env))
    for pattern in _SEARCH_GLOBS.get(lane, ()):
        search_dirs.extend(_expand(pattern))

    for directory in search_dirs:
        if lane == "java":
            # A glob hit for java is a JAVA_HOME, not a bin directory.
            home_dir = directory if (directory / "bin" / "javac").is_file() else (
                directory / "Contents" / "Home"
                if (directory / "Contents" / "Home" / "bin" / "javac").is_file() else None)
            if home_dir is not None:
                consider(home_dir / "bin", home_dir / "bin" / "javac", home_dir)
                continue
        if lane == "dotnet":
            consider(directory, directory / "dotnet")
        if not directory.is_dir():
            continue
        consider(directory, directory / exe_name)
        if lane == "python":
            for extra in sorted(directory.glob("python3.[0-9]")) + sorted(
                    directory.glob("python3.[0-9][0-9]")):
                consider(directory, extra)

    if lane == "python":
        # The interpreter running this collector is always a candidate, and on a minimal host it
        # is the only one. It is never PREFERRED for being ours -- it competes on its version.
        me = Path(sys.executable)
        if me.is_file():
            consider(me.parent, me)

    return sorted(found.values(), key=lambda c: c.version, reverse=True)


# --- selection --------------------------------------------------------------------------------

@dataclass
class Plan:
    """The outcome of resolution for one project root."""

    records: list[dict] = field(default_factory=list)
    overlay: dict[str, str] = field(default_factory=dict)
    interpreter: dict[str, str] = field(default_factory=dict)
    unsatisfied: list[str] = field(default_factory=list)


def _choose(windows: list[Interval], pool: list[Candidate],
            default: Candidate | None) -> Candidate | None:
    """Which installation to build with. None means the ask cannot be met here.

    THE DEFAULT WINS WHENEVER IT IS ALLOWED. This is not deference for its own sake: switching
    runtimes is itself a way to break a build that worked, and a repository whose declaration is
    the boilerplate `>=4.5` that a 2015 generator wrote does not want Node 4 in 2026. We move only
    when staying put would run a version the tree says it cannot use -- which is the whole of
    `wrong_runtime` and none of the risk.

    When we do move, the target is the candidate nearest the declared FLOOR. A project states a
    minimum because that is what it was built against; on this corpus newer is the failure.
    """
    allowed = [c for c in pool if satisfies(c.version, windows)]
    if not allowed:
        return None
    if default is not None and satisfies(default.version, windows):
        return default
    floor = windows[0].low if windows and windows[0].low else None
    if floor is None:
        return allowed[0]
    return min(allowed, key=lambda c: (c.version[:len(floor)], tuple(-n for n in c.version)))


def _default_of(lane: str, pool: list[Candidate], env: dict) -> Candidate | None:
    """The installation this host reaches without being told -- `which node`, `sys.executable`.

    Matched by resolved path against the pool, so it is the same object and carries the same
    version the search already established, rather than a second, possibly disagreeing probe.
    """
    if lane == "python":
        wanted = Path(sys.executable)
    else:
        found = which(_EXE[lane], env)
        if found is None:
            return None
        wanted = Path(found)
    try:
        target = wanted.resolve()
    except (OSError, ValueError):
        return None
    for candidate in pool:
        try:
            if candidate.executable.resolve() == target:
                return candidate
        except (OSError, ValueError):
            continue
    return None


def resolve(root: Path, env: dict, *, allow_home: bool = False,
            lanes: tuple[str, ...] | None = None) -> Plan:
    """Match this tree's declared runtimes against this host. Reads only; installs nothing.

    `lanes` narrows the work to the lanes a caller is about to use, which is what makes this cheap
    enough to run per project root: a Node project does not pay to enumerate the host's JDKs.
    """
    plan = Plan()
    budget = [_MAX_PROBES]
    for declaration in declarations(root):
        lane = declaration["lane"]
        if lanes is not None and lane not in lanes:
            continue
        every = candidates(lane, env, budget)
        pool = [c for c in every if allow_home or not c.under_home]
        # What a plain `node` / `python3` / `javac` resolves to right now, which is what the probe
        # would have used before this module existed. It stays the answer unless it is disallowed.
        default = _default_of(lane, pool, env)
        chosen = _choose(declaration["windows"], pool, default)
        present = default or (pool[0] if pool else None)
        record = {
            "lane": lane,
            "requested": declaration["requested"],
            "requested_source": declaration["source"],
            "used": None,
            "satisfied": None,
            "note": "",
        }
        if chosen is not None:
            record["used"] = ".".join(str(n) for n in chosen.version)
            record["satisfied"] = True
            record["note"] = f"the tree asks for {lane} {record['requested']} and this host has it"
            if chosen is not default:
                # Only an actual switch produces an overlay. Re-pointing PATH at what PATH already
                # resolves to would be a change with no effect and one more thing to explain.
                _apply(plan, lane, chosen)
                record["note"] += f", selected {record['used']} over the host default"
        elif present is None:
            record["satisfied"] = False
            record["note"] = (f"the tree asks for {lane} {record['requested']} and no {lane} "
                              f"runtime was found on this host, so this lane is a limit of the "
                              f"runner rather than a property of the repository")
            plan.unsatisfied.append(lane)
        else:
            record["used"] = ".".join(str(n) for n in present.version)
            record["satisfied"] = False
            record["note"] = (
                f"the tree asks for {lane} {record['requested']} and the closest this host offers "
                f"is {record['used']}, so any failure in this lane belongs to the runner rather "
                f"than to the repository")
            plan.unsatisfied.append(lane)
            if present is not default:
                # Still hand over the closest thing we have. A mismatched runtime sometimes builds
                # anyway, and a measurement we declined to attempt is worth less than one we
                # attempted and attributed honestly to ourselves.
                _apply(plan, lane, present)
        plan.records.append(record)
    return plan


def _apply(plan: Plan, lane: str, chosen: Candidate) -> None:
    """Record how to reach `chosen` from a child process, without disturbing anything else."""
    if lane == "python":
        # Python is invoked by absolute path, so it needs no PATH surgery at all -- which is also
        # why it is the lane that works identically under both trust settings.
        plan.interpreter[lane] = str(chosen.executable)
        return
    existing = plan.overlay.get("PATH")
    prefix = str(chosen.bin_dir)
    plan.overlay["PATH"] = prefix if existing is None else prefix + os.pathsep + existing
    if lane == "java" and chosen.home_dir is not None:
        plan.overlay["JAVA_HOME"] = str(chosen.home_dir)


def apply_overlay(env: dict, overlay: dict) -> dict:
    """`env` with `overlay` applied. PATH is PREPENDED so the host's tools stay reachable."""
    if not overlay:
        return env
    merged = dict(env)
    for key, value in overlay.items():
        if key == "PATH" and merged.get("PATH"):
            merged["PATH"] = value + os.pathsep + merged["PATH"]
        else:
            merged[key] = value
    return merged


def summarise(per_project: list[list[dict]]) -> dict:
    """The whole-tree runtime facts, in the shape the build record emits.

    Lists rather than one value per key: a polyglot tree asks for several runtimes and reporting
    only the first would hide the mismatch that actually stopped the build. Takes the per-project
    record lists rather than the Plans, because by the time this is called the plans are gone and
    the records are what got written down.
    """
    declared: list[str] = []
    unsatisfied: list[str] = []
    requested: list[str] = []
    used: list[str] = []
    notes: list[str] = []
    for records in per_project:
        for record in records or ():
            lane = record["lane"]
            if lane in declared:
                continue
            declared.append(lane)
            requested.append(f"{lane}@{record['requested']}")
            if record["used"]:
                used.append(f"{lane}@{record['used']}")
            if record["satisfied"] is False:
                unsatisfied.append(lane)
                notes.append(record["note"])
    return {
        "runtime_lanes_declared": declared,
        "runtime_lanes_unsatisfied": unsatisfied,
        "runtime_requested": requested,
        "runtime_used": used,
        "runtime_resolution_note": (
            "; ".join(notes[:3]) if notes else
            ("every runtime this tree declares is present on this host" if declared
             else "the tree declares no runtime version, so nothing was selected for it")),
    }


def which(name: str, env: dict) -> str | None:
    """`shutil.which` against a supplied environment's PATH, which is what the probe needs."""
    return shutil.which(name, path=env.get("PATH"))
