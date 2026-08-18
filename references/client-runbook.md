# Runbook for the repository owner

You own the repository and you own everything this produces. It runs on your machine, against your
checkout, with your own provider login. The four files it produces are written to a directory you
choose, and whether any of them is sent is your decision alone.

Two different things happen in a run and they need separate decisions:

1. **What the artifact contains.** Four files of numbers, enums, a digest and short model-written
   sentences. This part is tightly bounded and you can read all of it before deciding.
2. **What the run discloses on the way to producing it.** Unless you pass `--no-llm`, your source
   code and your full git history are read by a headless agent running under your own provider
   account. That is a real disclosure and it happens during the run, not when you send anything.

The order is: read this page, decide on point 2, run it, read the review, then decide on point 1.

Every claim below has a command next to it that checks the claim against the code you are about to
run. Run them from the `measure-ext/` directory. None of them touches your repository.

---

## 1. What it reads

| it reads | how |
|---|---|
| every file in the working tree at the checkout you name | a per-file line count, and a sha256 of every file's bytes for the content digest |
| the git history on the deepest reachable ref | `git log`, `git rev-list`, `git shortlog`, with fixed argument lists |
| your dependency manifests and lockfiles | to name ecosystems and frameworks |
| your CI configuration | to answer "is there CI, does it run tests" as two booleans |

Two things to be precise about, because earlier versions of this page were not.

**The digest reads every file, including a committed `.env`.** `repo_digest` in `measure.py` hashes
the bytes of every file in the tree so that the same tree always produces the same handle. A
`.env`, a `.pem` or an `id_rsa` inside the checkout is read as bytes and contributes to a sha256.
Nothing derived from its contents is emitted, and no value is retained anywhere. If that is still
unacceptable, remove those files from the checkout you point this at — which you should be doing
anyway, per section 2.

**The model lanes read your working tree and your development history.** The agent gets `Read`,
`Grep` and `Glob` over the checkout you named, and a pre-computed history written into a scratch
directory for the run and deleted at the end of it. It has no shell, so "whatever it decides to
read" is bounded by those two directories rather than by its own judgement. See section 2.

**Credential-shaped files are withheld from the history the agent reads.** The history is prepared
by `collectors/history_brief.py`, which excludes `.env`, `.netrc`, `.npmrc`, `*.pem`, `*.key`,
`id_rsa*`, `credentials*`, `secrets.*`, `*.keystore` and their siblings from every patch and every
file list, by git pathspec, before the agent sees any of it — content and filename both. It is the
mechanism, not a request in a prompt; the prompt says the same thing as a second layer.

```bash
# every git invocation in the skill, with its exact arguments
grep -rn '_git(\|run_git(\|"git",' collectors/ measure.py

# the digest, which is what reads every file in the tree
sed -n '/^def repo_digest/,/^    return h.hexdigest()/p' measure.py
```

## 2. What it executes

Four things.

**`git`,** with fixed argument lists and `shell=False`. No argument is ever assembled into a shell
string, so nothing in your repository's history can be interpreted as a command.

**The provider CLI you chose,** once for the material census and once for the mining lane. It is
launched as a fixed argument list. The exact invocations:

| provider | invocation |
|---|---|
| claude | `claude -p <prompt> --output-format json --add-dir <repo> --add-dir <history> --allowedTools "Read Grep Glob" --safe-mode --strict-mcp-config --no-session-persistence --model <id>` |
| codex | `codex exec --ephemeral --sandbox read-only --skip-git-repo-check --ignore-rules -c project_doc_max_bytes=0 --model <id> --output-last-message <file> -` |
| gemini | not supported for the model lanes — see below |

Three things in those rows are the security posture, and each is a control the CLI enforces rather
than an instruction in a prompt.

- **The agent has no shell.** The grant is `Read Grep Glob`. `--add-dir` bounds those three, so the
  agent can read your checkout and the prepared history and nothing else. It previously had `Bash`,
  which `--add-dir` does not bound: a shell command reached anything on your filesystem that you
  can reach, and the only thing stopping it was the model choosing not to. That grant is withdrawn.
- **Your repository cannot configure the agent that measures it.** `--safe-mode` turns off every
  customisation the CLI would otherwise load — `CLAUDE.md`, hooks, MCP servers, plugins, skills,
  custom agents and commands — from every settings source, and `--strict-mcp-config` allows no MCP
  server we did not pass, which is none. Without those flags a `.claude/settings.json` committed to
  the tree supplies hooks and the CLI runs them: a shell command, on your machine, as you, with no
  `--build` and no prompt. Model selection, authentication and the built-in tools are unaffected.
- **No session transcript is written.** `--no-session-persistence` for `claude`, `--ephemeral` for
  `codex`. Nothing the agent read is left in your home directory. See section 5.

The history the withdrawn shell used to fetch is prepared before the session starts, by
`collectors/history_brief.py`, using a fixed read-only `git` argv that carries `--no-textconv` and
`--no-ext-diff` — because `.git/config` can name a `textconv` or external diff driver and `git show`
will then run it, which is a second way a repository executes code on the machine reading it. The
brief goes into a scratch directory, is handed to the agent read-only, and is deleted when the lane
finishes.

**What that cost, measured rather than assumed.** Both lanes were run over the same real repository
(`pallets/click`, 3,311 commits, 687 of them substantive) with the same model and the same
isolation flags, differing only in the tool grant. Five census runs and three mining runs, because
one of each would not have separated the effect from the model's own run-to-run variation:

| census run | instances | weighted total | `logic_depth` | `defect_repairs` | `complex_logic` |
|---|---|---|---|---|---|
| with `Bash` | 71 | 58.9 | 6 | 12 | 11 |
| with `Bash`, again | 81 | 65.4 | 6 | 12 | 12 |
| no `Bash` | 63 | 50.6 | 5 | 8 | 10 |
| no `Bash`, again | 73 | 57.3 | 6 | 9 | 10 |
| no `Bash`, 450 patches instead of 160 | 64 | 52.3 | 5 | 8 | 11 |

| mining run | candidates | grounded in a real commit |
|---|---|---|
| with `Bash` | 42 | 33 |
| no `Bash` | 40 | 40 |
| no `Bash`, 450 patches | 43 | 43 |

Read plainly: **mining is not degraded** — the candidate count is unchanged and every candidate now
cites a real commit, because the agent is reading a table of commits rather than remembering a
scrolled `git log`. **The census loses about 14% of its weighted total**, consistently and in one
direction, and `logic_depth` fell one band on two of the three runs. The whole of that loss sits in
`defect_repairs`, the category that most depends on ranging freely over history; every present-tree
category is inside the run-to-run spread. Materialising 450 patches instead of 160 did not recover
it, so it is not a volume problem — it is the loss of ad-hoc querying, and it is the price of the
grant being a control rather than a preference.

If that level shift matters for your comparison, the narrowest alternative that keeps a shell is
`--allowedTools "Read Grep Glob Bash(git log:*)"`, and it is a real control: measured on this
machine, it refused `ls $HOME`, `cat $HOME/<file>`, `env` and every compound command. It is not
shipped, for two reasons that are also measured. The CLI's built-in set of harmless commands still
permits `whoami`, so the boundary has a carve-out we do not define. And `git show` executes
commands named by the repository's own `.git/config` through a `textconv` driver, which makes an
allowlist containing it a remote code execution primitive rather than a read-only grant.

**Under `gemini`, the model lanes refuse to run.** That is not an oversight and it is not
temporary: the gemini CLI has no way to ignore configuration supplied by the repository it is
pointed at. Its only mechanism is folder trust, which is all-or-nothing — and a folder marked
untrusted makes a headless `-p` run refuse to start at all. In a trusted folder the repository's
`.gemini/settings.json` supplies hooks and MCP servers, its `GEMINI.md` is injected into the prompt,
and its `.gemini/.env` can set the system-prompt override. There is no `--safe-mode`, no
`--setting-sources`, no way to disable context-file discovery from the command line and no way to
stop it writing a session transcript. Verified against the gemini CLI source at v0.54.4. Use
`claude` or `codex`, or `--no-llm`.

**Your project's own install, build and test commands** — but only if you pass `--build`. With it,
your commands run in the checkout you named, and dependency installation runs too: `npm install`,
`pip install`, `cargo fetch`, `go mod download`, `bundle install`, `composer install`,
`dotnet restore` and the rest, depending on what your tree declares. A bare `--build` stops after
installing, building and asking your test runner to LIST its tests; `--build full` also executes
the suite. Caches use scratch storage
where the ecosystem supports it, the child gets a scratch `HOME` and no credential of any kind, and
files that installers are known to rewrite are restored afterwards. **The checkout is still
modified.** Anything an install script creates outside that known list stays where it landed.
Treat the checkout as consumed by the run.

**And the deterministic collectors themselves,** which are Python subprocesses of this skill running
this skill's own code.

```bash
# no command is ever handed to a shell   (expected: no output)
grep -rn 'shell=True' collectors/ miners/ measure.py

# every subprocess this skill starts
grep -rn 'subprocess\.run(\|subprocess\.Popen(' collectors/ miners/ measure.py

# the three that inherit YOUR environment rather than a built one, in full. All three are
# `git` with read-only subcommands; every model child and every build child gets a built
# environment instead. This is the exception, stated rather than papered over.
sed -n '/^def run_git/,/^        return ""/p'      collectors/git_stats.py
sed -n '/^def _git(/,/^    return r.stdout/p'      collectors/git_history.py
sed -n '/^def head_sha/,/^    return out.stdout/p' miners/agent_io.py

# what the model child is and is not given
sed -n '/^def build_env/,/^    return _assert_clean/p' collectors/childenv.py
```

## 3. What leaves your machine

Two separate flows, in order of how much leaves.

**Your source code and history leave, to your model provider, during the run.** Unless you pass
`--no-llm`, the census and the mining lane show the agent your working tree and your development
history. Everything the agent reads is sent to your provider's API under your own account, on your
own login, exactly as it would be if you opened the same repository in the same assistant by hand.
Assume anything in the working tree is seen.

Credential-shaped files are the one carve-out, and it is enforced rather than requested: the
history the agent is given excludes `.env`, `.netrc`, `.npmrc`, `*.pem`, `*.key`, `id_rsa*`,
`credentials*`, `secrets.*` and their siblings by git pathspec, so a secret committed at any point
in the history does not reach the provider through a diff and its filename is not listed either. A
credential sitting in the *working tree* is a different matter — the agent can open any file in the
checkout — so remove those from the disposable clone you point this at, as section 7 says. If none
of this is acceptable, run with `--no-llm`, which calls no model at all.

**The four artifact files do not leave.** This skill opens no network connection of its own. It
imports no HTTP client, no socket library and no cloud SDK. The files are written to your output
directory and stay there. Sending them is a separate act that you perform.

**With `--build`, package registries are contacted.** Dependency installation fetches from
`registry.npmjs.org`, PyPI, crates.io, Maven Central and whatever else your manifests name,
including any private index your tree configures. Install scripts your repository declares run with
network access. This is your own build doing what your own build does, but it is outbound traffic
and it is not the provider CLI, so it belongs in this section.

```bash
# no HTTP client, no socket, no cloud SDK is imported anywhere   (expected: no output)
grep -rnE '^\s*(import|from)\s+(requests|urllib|httpx|http|socket|aiohttp|boto3)' \
  collectors/ miners/ measure.py

# nothing loads an environment file   (expected: no output)
grep -rn 'dotenv\|load_dotenv' collectors/ miners/ measure.py

# the dependency-install commands --build will run
grep -n 'install\|fetch\|restore\|download' collectors/build_probe.py | grep '\["' | head -30
```

**What can be in the four files is fixed in code,** in `collectors/emit_allowlist.py`. Every field is
declared there with the kind of value it may hold, and a value — or a map key — that is not one of
those kinds stops the run instead of being written. There are four kinds: numbers, booleans and
closed-vocabulary enums; the content digest and the `repo-<digest>` handle derived from it; short
validated sentences of model-written prose; and nothing else.

```bash
# the complete declaration of what may be written, as one readable file
less collectors/emit_allowlist.py
```

## 4. What is never collected

This table is about the artifact. Section 3 is about the run, and the two are different questions.

| never in the artifact | why |
|---|---|
| credentials, keys, tokens, passwords | nothing in this tool searches your source for secret-shaped strings, and no such string is ever matched, counted or emitted |
| author names and email addresses | history is counted, not attributed |
| commit subjects and messages | your commit prose is yours |
| branch and tag names | release and branch names routinely carry product identity |
| file and directory paths, the repository name | the artifact names no part of your tree |
| environment variable names | the names alone describe your infrastructure |

There is one narrow exception to the first row and it is disclosed here rather than left to be
found. **With `--build` only,** if a dependency install fails with an authentication error, the
build probe checks whether your checkout itself declares a private package index — because "this
repository needs an internal registry" and "this operator's own npm login is stale" are opposite
findings that produce the same error. That check reads registry-configuration markers from
`.npmrc`, `.yarnrc`, `.yarnrc.yml`, `pip.conf`, `poetry.toml`, `settings.xml`, `nuget.config`,
`gradle.properties` and `.netrc` **inside the checkout**, and two of those markers are the words
`_authToken` and `password`. It never looks at the equivalent files in your home directory, it
retains nothing it read, and its entire output is one boolean that changes how a build failure is
attributed. It runs nowhere else and never without `--build`.

```bash
# no secret-shaped pattern exists in the skill to match your source against
#   (expected: no output)
grep -rniE 'AKIA|ghp_|xoxb-|\bsk-[a-z0-9]{16}|BEGIN [A-Z ]*PRIVATE KEY|secret_pattern' \
  collectors/ miners/ measure.py

# the one credential-adjacent read, in full: what it opens, what it returns
sed -n '/^_REGISTRY_CONFIG/,/^    return False/p' collectors/build_probe.py
grep -n '_declares_private_registry' collectors/build_probe.py

# author name and address are read at the git parse site, converted to a salted key
# and a bot flag, and go out of scope on the next line
grep -n 'author_key\|author_is_bot' collectors/git_stats.py
```

The model-written sentences are checked before they are written: a sentence containing a path, a
file name, a symbol, a dotted or snake_case identifier, an acronym, a quoted string, or a
capitalised product or company name is dropped whole. The idea is still counted; it arrives without
a description. Nothing is emitted half-redacted.

## 5. What the run leaves behind on your machine, outside the output directory

Nothing, and that is a change from earlier versions of this tool, so here is how to check it rather
than take it.

The provider CLI writes a verbatim session transcript by default — every file body the agent
opened, every diff it read — into `~/.claude/projects/<slugged path of the checkout>/`. On an
eight-line repository that was roughly 200 KB of unredacted source and history sitting outside the
output directory you were told about. The model lanes now pass `--no-session-persistence`
(`--ephemeral` for `codex`), so no transcript is written at all.

```bash
# before the run
find ~/.claude/projects -name '*.jsonl' | wc -l
# ...run measure.py...
# after: the same number
find ~/.claude/projects -name '*.jsonl' | wc -l

# and the flag that does it, in the argv the census actually builds
grep -n 'isolation_flags\|ISOLATION' collectors/childenv.py | head
```

The prepared git history lives in a temporary directory for the length of the lane and is deleted
when the lane returns. The four artifact files are the only thing this skill writes that outlives
the run, and it removes nothing you or your provider produced.

## 6. How long it takes

Independent lanes run concurrently, so the wall clock is the slowest lane rather than the sum of all
of them. The two model lanes are the long poles and they overlap the file and history reads
entirely. `--build` is the exception: it runs alone, after every reader has finished, because it
changes the tree the others are reading.

**One repository cannot take longer than `--budget-seconds`, which defaults to two and a half
hours.** One clock covers the whole run and every lane draws from it. A small repository still
finishes in a few minutes; a large one spends the budget rather than exceeding it.

Before any work starts, the run prints what it expects to cost:

```
[plan] 2 project roots (python), 114 files in 22 directories scanned
[plan] this looks like a 1 minute run against a 150 minute budget (rough, from repository size and
       project count): deterministic collectors 0 min, build probe 1 min
```

If the estimate does not fit the budget, that line says so and names what will be cut, so you are
never surprised by a shortened run. Progress is printed while lanes run and a per-lane wall-clock
table is printed at the end, so a long run is visibly alive rather than apparently hung.

**Nothing is truncated to fit a clock without saying so.** Whatever the budget does not reach
reports every measurement it owns as null with a reason: the build probe marks project roots it
never got to as `skipped_budget`, and the runnability index goes null rather than adding up terms
nobody measured. It never reports a partial count, a zero, or a low score. A repository that could
not be measured is reported as unmeasured, never as a poor one.

To bound the run more tightly, pass a smaller `--budget-seconds`. The consequence is the same:
nulls with a reason, not a worse-looking repository.

## 7. Run it

Use a clean, disposable clone or worktree, and keep the output directory outside that checkout so
the artifact cannot alter the tree it measured.

Agent configuration committed to your repository — a `CLAUDE.md`, a `.claude/settings.json`, a
`.mcp.json`, an `AGENTS.md` — is ignored by the run and cannot execute or steer anything. You do not
have to audit the checkout for it first. If you want to see that for yourself:

```bash
# the flags that make the CLI ignore it, and the check that stops the run if the
# installed CLI is too old to offer them
sed -n '/^ISOLATION/,/^}/p' collectors/childenv.py
```

Then:

```bash
python measure.py <disposable-repo-checkout> \
  --out <output-directory-outside-repo> \
  --build full \
  --provider claude \
  --model <approved-model>
```

This is the invocation to use when the measurement needs to be comparable with others. It is also
the widest-reaching one: it runs the model lanes and it runs your build.

- `--build` collects dependency-install, build and test-discovery evidence; `--build full` adds
  test execution and coverage, which is what the runnability score needs. Without either, those
  columns are null and the resulting grade is marked not comparable. With either, your dependencies
  are fetched from the network and your install scripts run. The checkout is modified and should be
  discarded afterwards.
- `--provider claude` is explicit even though it is the default. `codex` is equally supported and
  also needs an explicit `--model`; `gemini` is not, for the reason in section 2. A provider CLI
  that is not installed, or one too old to be isolated from your repository's own configuration,
  stops the run immediately with a one-line message. It never carries on and quietly reports your
  repository as unmeasurable, and it never measures a repository that is configuring its own
  measurement.
- The census and mining lanes let the provider CLI read your source and your development history,
  under your own login, on your machine, with no shell and no access outside the checkout. Section
  2 and section 3 are the disclosure. Decide on them deliberately, before you run this.
- Add `--jobs 1` to serialise the lanes if you would rather the run used one core.

## 8. Read the review

Every run ends by printing a REVIEW of exactly what is in the four files:

- how many numbers, booleans, closed-vocabulary values and timestamps there are;
- the content digest and the `repo-<digest>` handle, in full;
- every mined task idea, one line each, verbatim;
- every other piece of model-written prose, verbatim. That is: the domain themes, the material
  summary paragraph, the one-line note on what the census could not assess, and one sentence for
  each piece of substantial material the census enumerated. On a large repository the last of these
  can run to many sentences — the count for each category is the number of sentences in it, so the
  numbers can be audited by reading the evidence behind them;
- this tool's own status notes;
- every field that ships with no value, and every field that is never filled in, each with its
  reason.

The mined ideas are descriptions of *exercises a coding model could be asked to perform against a
copy of a codebase like yours* — a capability the codebase does not have yet, a defect its own
history fixed, a structural change such as a migration or an interface redesign, or a long-horizon
task that is none of those. Nothing in this tool ever writes to your repository, applies a change,
or acts on any of these ideas. They are shapes of work, recorded as text.

For the complete field-by-field listing, run the same command again with `--review`, or run the fast
deterministic pass and read that:

```bash
python measure.py <checkout> --out <output-directory> --no-llm --review
```

You can also read the artifact directly. Every string in it, with its location:

```bash
python -c '
import json, pathlib, sys
def walk(o, p=""):
    if isinstance(o, dict):
        for k, v in o.items(): yield from walk(v, f"{p}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o): yield from walk(v, f"{p}[{i}]")
    elif isinstance(o, str):
        yield p, o
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
    for p, s in walk(json.loads(f.read_text()), f.name):
        print(f"{p} = {s}")
' <output-directory>
```

On a real repository that is a few dozen to a few hundred strings, and they should all be technology
names, timestamps, digests, statuses, this tool's own provenance text, and model-written prose that
names no file, symbol, product or person.

## 9. Decide

The four files that make up a complete measurement are:

- `measurement.json`
- `codebase_repos.json`
- `codebase_repos.csv`
- `codebase_repo_mining.json`

Grading combines `measurement.json` and `codebase_repos.json` automatically when they are kept side
by side. Keep your own copy. Nothing here overwrites or discards anything after the run, and you are
free to open, diff, or archive the files first.

If any single field is unacceptable to you, say which one. The declaration table it comes from is
one readable file, and a field can be turned off there without changing anything else.

## 10. The questions this page cannot answer

Everything above is a fact about code you can read. The following are not, and no skill can settle
them. They are contractual, and they should be settled in writing before you send anything.

- Where does the artifact go once you send it, and in what system is it stored?
- Who inside the receiving organisation can read it?
- How long is it retained, and under what schedule is it deleted?
- Can you require its deletion later, by what mechanism, and how is that confirmed to you?
- Is there a data-processing agreement covering it, and does it cover this artifact specifically?

There is one related question this page *can* answer, and the answer is narrow: the census and
mining lanes call the provider CLI you already have installed, with your own credentials, under your
own account. Whatever terms already govern your use of that account — retention, training, human
review — are the terms that govern what the agent sees. This tool does not add an account, a key, or
a route of its own, and it never sees your provider credential's value; it copies the variable by
name into the child process (`collectors/childenv.py`). If those terms are not acceptable for this
source tree, `--no-llm` calls no model at all.

## Source-free diagnostic mode

```bash
python measure.py <repo-checkout> \
  --out <output-directory-outside-repo> \
  --build \
  --no-llm
```

No model is called at all, so no provider CLI reads your source and nothing about your repository
leaves the machine even transiently. It still collects the deterministic and environment evidence, but the
material dimension is absent. The run therefore reports `status: partial` with
`skip_reason: llm_disabled` on the flat row, and the grade produced from it is marked
`grade_status: UNGRADED` and `score_comparable: false`. Do not use that result for purchasing or
ranking.

Any run with a gap in it says so at the top level, not only in a nested block: `status` is
`measured` only when every lane ran. `skip_reason` distinguishes a lane you turned off
(`llm_disabled`, `mine_disabled`) from one that could not run (`lanes_unavailable`) or ran out of
clock (`lanes_timed_out`).
