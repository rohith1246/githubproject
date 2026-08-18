#!/usr/bin/env python3
"""code_structure.py -- where complexity sits, and how much of it there is.

Contributes three things:

  * `decisions_gini_top1pct` -- what share of all decision points in the repository sit in its
    densest 1% of functions. This is the measurement that justifies the parser dependency: it
    is the only structural figure we have found that is genuinely independent of repository
    size. A large codebase and a small one can score identically, and the difference it
    captures -- complexity gathered into a few real modules versus smeared evenly across many
    shallow files -- is invisible to every size-based metric.
  * `error_handling_per_kloc` -- try/catch/Result density in production code. Code that states
    its failure modes has failure modes worth exercising.
  * `prod_loc` and `source_files` -- size controls. Never scored on directly; they exist so the
    other measurements can be read in proportion to how much code there is.

Attributing decisions to functions needs real function boundaries, which is why tree-sitter is
a dependency rather than a regex. If the parser is unavailable this module degrades: the
structure criteria are reported UNSCORED rather than zero, because a missing parser is a fact
about the operator's machine and not a property of the repository.

Test files, vendored trees, build output and generated code are excluded throughout. Counting
them would let a repository look substantial when most of it is machinery.

Language coverage is in three layers, because `source_files == 0` and `prod_loc < 500` are hard
no-buy gates and an unrecognised language would therefore be indistinguishable from an empty
repository -- a blind spot in this file wearing the costume of a verdict about somebody's code:

  1. EXT_LANG -- extensions the pinned parser pack can parse. Counted and attributed.
  2. EXT_UNPARSED -- real languages with no grammar. Counted, never parsed, and reported
     separately as `source_files_unparsed` so the reader can see what the concentration figure
     was computed over.
  3. an agentic second opinion, asked ONLY when the deterministic scan is about to fire one of
     those gates on a tree that held substantial text it could not classify. It can withdraw a
     zero to None -- unmeasured, never zero -- and it can never invent a number.
     `structure_probe_mode` always says which of these happened.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import childenv

# Extension -> tree-sitter language name.
#
# Every name here was verified to load with `get_parser(name)` against the pinned
# tree-sitter-language-pack; a name the pack cannot load raises once per file, counts as a parse
# failure, and quietly costs the repository its concentration measurement. The map is wide on
# purpose. A language missing from it makes a repository full of code look like it has none, and
# `source_files == 0` is a hard no-buy gate in rubric.py -- so a gap here does not shade a score
# down, it manufactures a rejection. That already happened once, to a repository of twenty
# Jupyter notebooks, before `.ipynb` was added.
#
# Deliberately absent: data, markup, config and schema files -- json, yaml, toml, xml, csv,
# markdown, html, css, and the IDL family (proto, thrift, graphql, prisma, capnp). None of them
# is code, and counting them would defeat the zero-code gate they would otherwise be firing
# under. Build and infrastructure DSLs (terraform/hcl, cmake, dockerfile, nix, bazel) are
# excluded on the same reading: they are configuration expressed in a grammar, and a tree
# holding nothing else is not a tree a coding task can be mined from.
EXT_LANG = {
    # --- the mainstream, unchanged in meaning from the original table -----------------
    ".py": "python", ".pyw": "python", ".pyi": "python", ".ipynb": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".es6": "javascript", ".gs": "javascript",   # .gs is Google Apps Script, which is JS
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala",
    ".rb": "ruby", ".rake": "ruby", ".gemspec": "ruby",
    ".php": "php", ".phtml": "php",
    ".cs": "c_sharp", ".csx": "c_sharp", ".rs": "rust", ".swift": "swift",
    ".c": "c", ".h": "c",                        # .h stays C: guessing C++ from a header is worse
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".h++": "cpp", ".ipp": "cpp", ".tpp": "cpp",
    ".cu": "cuda", ".cuh": "cuda", ".ino": "arduino",
    ".mm": "objc",                               # Objective-C++; the objc grammar covers most of it
    ".lua": "lua", ".luau": "luau", ".dart": "dart",
    # --- BEAM and functional -----------------------------------------------------------
    ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang", ".hrl": "erlang", ".escript": "erlang",
    ".gleam": "gleam", ".elm": "elm",
    ".hs": "haskell", ".lhs": "haskell", ".purs": "purescript",
    ".ml": "ocaml", ".mli": "ocaml_interface",
    ".fs": "fsharp", ".fsx": "fsharp", ".fsi": "fsharp_signature",
    ".sml": "sml", ".idr": "idris", ".lidr": "idris",
    ".agda": "agda", ".lagda": "agda", ".lean": "lean", ".roc": "roc", ".gren": "gren",
    # --- lisps. They parse, but see the note on _FUNC: an s-expression grammar has no
    #     function node, so these contribute files and lines without attribution.
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure",
    ".lisp": "commonlisp", ".lsp": "commonlisp", ".cl": "commonlisp", ".asd": "commonlisp",
    ".el": "elisp", ".scm": "scheme", ".ss": "scheme", ".sld": "scheme", ".rkt": "racket",
    ".fnl": "fennel", ".janet": "janet", ".hoon": "hoon",
    # --- scientific and statistical ----------------------------------------------------
    ".jl": "julia", ".r": "r", ".stan": "stan",
    ".f90": "fortran", ".f95": "fortran", ".f03": "fortran", ".f08": "fortran",
    ".f": "fortran", ".for": "fortran", ".f77": "fortran", ".ftn": "fortran", ".fpp": "fortran",
    # --- the legacy enterprise tail ----------------------------------------------------
    ".cbl": "cobol", ".cob": "cobol", ".cpy": "cobol",
    ".ada": "ada", ".adb": "ada", ".ads": "ada",
    ".pas": "pascal", ".pp": "pascal", ".dpr": "pascal", ".dpk": "pascal", ".lpr": "pascal",
    ".vb": "vb", ".vbs": "vb", ".bas": "vb",
    ".cls": "apex", ".trigger": "apex", ".apex": "apex",   # Salesforce, not Progress ABL
    ".bsl": "bsl", ".magik": "magik", ".cfc": "cfml", ".cfm": "cfml",
    # --- scripting ---------------------------------------------------------------------
    ".pm": "perl", ".perl": "perl",              # .pl is ambiguous; see _AMBIGUOUS
    ".prolog": "prolog", ".groovy": "groovy", ".gvy": "groovy",
    ".tcl": "tcl", ".tk": "tcl", ".awk": "awk", ".vim": "vim",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".sh": "bash", ".bash": "bash", ".ksh": "bash", ".csh": "bash", ".tcsh": "bash",
    ".zsh": "zsh", ".fish": "fish", ".nu": "nushell", ".elv": "elvish",
    ".bat": "batch", ".cmd": "batch",
    ".jq": "jq", ".rego": "rego", ".prql": "prql", ".ql": "ql", ".qll": "ql",
    ".mojo": "mojo",
    # --- systems -----------------------------------------------------------------------
    ".nim": "nim", ".nims": "nim", ".nimble": "nim",
    ".zig": "zig", ".cr": "crystal", ".d": "d", ".odin": "odin",
    ".hx": "haxe", ".hack": "hack", ".hhi": "hack", ".pony": "pony",
    ".jai": "jai", ".ha": "hare", ".c3": "c3", ".nut": "squirrel", ".ck": "chuck",
    ".gd": "gdscript", ".as": "actionscript", ".brs": "brightscript",
    ".res": "rescript", ".resi": "rescript", ".smali": "smali",
    ".ll": "llvm", ".mlir": "mlir", ".wat": "wat", ".wast": "wast", ".tal": "uxntal",
    ".s": "asm", ".asm": "nasm", ".nasm": "nasm", ".masm": "x86asm",
    # --- hardware and shaders ----------------------------------------------------------
    ".vhd": "vhdl", ".vhdl": "vhdl", ".vh": "verilog",
    ".sv": "systemverilog", ".svh": "systemverilog",
    ".glsl": "glsl", ".vert": "glsl", ".frag": "glsl", ".geom": "glsl", ".comp": "glsl",
    ".tesc": "glsl", ".tese": "glsl", ".hlsl": "hlsl", ".fx": "hlsl", ".wgsl": "wgsl",
    ".scad": "openscad",
    # --- smart contracts ---------------------------------------------------------------
    ".sol": "solidity", ".move": "move", ".cairo": "cairo", ".clar": "clarity",
    ".tact": "tact", ".sw": "sway", ".fc": "func", ".circom": "circom",
    # --- SQL. Included because stored procedures and PL/SQL packages are real logic, which
    #     is why PL/SQL and T-SQL belong on a list of languages rather than of data formats.
    ".sql": "sql", ".pls": "sql", ".plsql": "sql", ".pks": "sql", ".pkb": "sql",
    ".prc": "sql", ".fnc": "sql", ".tsql": "sql", ".ddl": "sql",
    # --- single-file component formats. These grammars treat the embedded script as opaque
    #     text, so they yield no function boundaries -- but a Vue or Svelte application is
    #     entirely code, and omitting them would fire the zero-code gate on one.
    ".vue": "vue", ".svelte": "svelte", ".astro": "astro", ".razor": "razor",
    # --- remaining odds and ends -------------------------------------------------------
    ".st": "smalltalk", ".forth": "forth", ".fth": "forth", ".4th": "forth", ".dl": "souffle",
}

# A few extensions belong to more than one living language, and choosing wrong costs the
# repository its concentration measurement while still counting its lines. Each resolver reads
# the file's own bytes; the fallback is whichever reading is commoner in real repositories.
def _resolve_m(data: bytes) -> str:
    # .m is Objective-C or MATLAB. Objective-C is unmistakable from its directives.
    if re.search(rb"^[ \t]*(#import|#include|@interface|@implementation|@end)", data, re.M):
        return "objc"
    return "matlab"


def _resolve_v(data: bytes) -> str:
    # .v is Verilog, V, or Coq. `endmodule` is Verilog and nothing else. Coq is parsed as V and
    # will yield few functions -- that costs attribution, never the file or line count.
    if re.search(rb"\bendmodule\b", data):
        return "verilog"
    return "v"


def _resolve_pl(data: bytes) -> str:
    # .pl is Perl or Prolog. A Prolog source is built from `head :- body.` clauses.
    if re.search(rb"^[ \t]*[a-z][A-Za-z0-9_]*[ \t]*(\(.*\))?[ \t]*:-", data, re.M):
        return "prolog"
    return "perl"


_AMBIGUOUS = {".m": _resolve_m, ".v": _resolve_v, ".pl": _resolve_pl}

# A notebook is JSON on disk and code to everyone who works on it. Counting the raw JSON would
# inflate the line count with output blobs and base64 images, and skipping the extension
# altogether reports a repository of twenty notebooks as having no code at all -- which then
# fires the zero-code no-buy gate on a codebase that is entirely code.
def _notebook_source(data: bytes) -> bytes:
    try:
        nb = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return b""
    out: list[str] = []
    for cell in (nb.get("cells") or []):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        src = cell.get("source")
        if isinstance(src, list):
            out.append("".join(str(x) for x in src))
        elif isinstance(src, str):
            out.append(src)
    return ("\n".join(out)).encode("utf-8", "replace")

# These three anchor on `/`, which is why every path they see is put through `Path.as_posix()`
# first. rglob yields `\` separators on Windows, so matching the native separator would make
# `(^|/)` never fire there: vendored trees would be counted as first-party and test files as
# production code, on that platform only, with no error to show for it.
_SKIP_DIR = re.compile(
    r"(^|/)(node_modules|bower_components|vendor|third_party|thirdparty|dist|build|out|"
    r"target|bin|obj|\.git|__pycache__|\.venv|venv|env|site-packages|coverage|"
    r"migrations|generated|gen|\.next|\.nuxt|\.terraform|Pods)/", re.I)
_SKIP_FILE = re.compile(r"\.(min|bundle|generated|pb|_pb2|d)\.[a-z]+$", re.I)
# `testdata` is Go's convention and `spec`/`__tests__` the JS ones; the trailing alternative
# catches `foo_test.go`, `foo.test.ts` and `foo-spec.rb` without matching `latest.py`, which a
# looser pattern would swallow whole.
_TEST = re.compile(r"(^|/)(tests?|spec|specs|__tests__|__mocks__|e2e|fixtures|testdata)/|"
                   r"(^|/)(test_|conftest\.)|[._-](test|spec)\.[a-z0-9]+$", re.I)
_GENERATED_HDR = re.compile(rb"@generated|DO NOT EDIT|Code generated by|autogenerated", re.I)

# Node types representing a branch, a loop, or a guarded path -- a decision the code makes.
# Spelt broadly because grammars disagree on names for the same construct, and enumerated from
# the grammars' own node vocabularies rather than guessed: every name below was read out of
# `Language.node_kind_for_id` for at least one language in EXT_LANG.
#
# Over-counting a single construct (a `switch_statement` that also contains `switch_case` nodes)
# is harmless here on purpose: `decisions_gini_top1pct` is a SHARE, so a factor that applies
# uniformly across a language cancels in the ratio. Under-counting is what costs the
# measurement, so the set errs wide. Names that merely mention a keyword without being a branch
# -- Elm's `lower_case_identifier`, SQL's `keyword_case`, C's `preproc_if`, type-level spellings
# like `conditional_type` and `match_type` -- are excluded, because those inflate one language's
# count relative to its own function bodies rather than uniformly.
_DECISION = {
    # --- the original set, unchanged ----------------------------------------------------
    "if_statement", "if_expression", "if", "elif_clause", "elsif", "else_clause", "unless",
    "for_statement", "for_in_statement", "for_of_statement", "for_expression", "for",
    "while_statement", "while_expression", "while", "do_statement", "loop_expression",
    "switch_statement", "switch_expression", "case_statement", "case", "when_clause", "when",
    "match_statement", "match_expression", "catch_clause", "except_clause", "rescue",
    "conditional_expression", "ternary_expression", "boolean_operator", "guard_statement",
    "select_statement", "type_switch_statement", "try_statement",
    # --- branch, in other grammars' spellings -------------------------------------------
    "if_expr", "if_clause", "if_stmt", "if_block", "if_then_else", "if_else_expr", "exp_if",
    "if_case_statement", "multi_way_if", "single_line_if", "multi_line_if", "cond_exp",
    "conditional", "conditional_statement", "conditional_declaration", "static_if_statement",
    "elif", "elif_expression", "elseif", "elseif_clause", "elseif_statement", "elseif_block",
    "else_if_clause", "else_if_statement", "else_if_expr", "else_if", "if_header",
    "elsif_statement_item", "elsif_expression_item", "else_statement", "else_block",
    "else_part", "else_expression", "else_if_header", "unless_modifier", "if_modifier",
    "modifier_if", "modifier_unless", "arithmetic_if_statement",
    # --- multi-way dispatch --------------------------------------------------------------
    "switch_stmt", "switch", "switch_case", "switch_default", "switch_entry", "switch_match",
    "case_clause", "case_expression", "case_expr", "case_exp", "case_item", "case_stmt",
    "case_of_expr", "case_of_branch", "case_pattern", "case_match", "case_else_block",
    "match", "match_arm", "match_block", "match_case", "match_alt", "match_branch",
    "match_pattern", "match_expr", "when_expression", "when_entry", "when_statement",
    "when_is_expr", "when_other", "select_case_statement", "select_expression", "evaluate_header",
    "case_statement_alternative", "case_expression_alternative", "select_type_statement",
    "ofBranch", "caseStmt", "SwitchExpr", "SwitchProng",
    # --- iteration -----------------------------------------------------------------------
    "for_stmt", "for_expr", "for_clause", "for_loop", "for_block", "for_in_clause",
    "for_range_loop", "foreach", "foreach_statement", "foreach_stmt", "for_each",
    "for_each_statement", "for_each_in_statement", "for_each_loop", "enhanced_for_statement",
    "for_generic_clause", "for_numeric_clause", "generic_for_statement",
    "numeric_for_statement", "cstyle_for_statement", "c_style_for_statement", "search_statement",
    "for_generate_statement", "loop_statement", "loop", "do_loop", "do_while_statement",
    "do_while_expression", "do_until_statement", "repeat_statement", "repeat", "until",
    "until_statement", "while_stmt", "while_loop", "repeat_while_statement", "iterate_statement",
    "iteration_scheme", "while_modifier", "until_modifier", "do_stmt", "do_group",
    "perform_statement_loop", "perform_varying", "comprehension_for", "comprehension_if",
    "forStmt", "whileStmt", "ForStatement", "WhileStatement", "LoopStatement",
    # --- failure paths -------------------------------------------------------------------
    "try_expression", "try_expr", "try_block", "try_catch", "try", "catch", "catch_block",
    "catch_statement", "catch_expr", "rescue_block", "rescue_modifier", "modifier_rescue",
    "ensure", "finally_clause", "except_group_clause", "seh_try_statement", "seh_except_clause",
    "catch_unwrap", "try_unwrap", "scope_guard_statement", "tryStmt", "tryExceptStmt",
    # --- guards ---------------------------------------------------------------------------
    "guard", "guard_clause", "guard_pattern", "pattern_guard", "if_guard", "unless_guard",
    "guard_equation", "match_guard", "conditional_execution",
    # --- shell and Nushell control words --------------------------------------------------
    "ctrl_if", "ctrl_match", "ctrl_for", "ctrl_while", "ctrl_loop", "ctrl_try",
    "IfStatement", "ifStmt", "elifStmt", "elseStmt", "inlineIfStmt", "inlineTryStmt",
}
# The unit decisions are attributed to. Precision matters more here than in _DECISION: a name
# that matches a construct nested INSIDE a function steals that function's decisions and
# fragments the distribution, so `*_body`, bare `block`, and `*_type` spellings are all absent.
#
# A language gets an entry here only if _DECISION also covers it. Adding a function node for a
# language whose branches we cannot see would emit a stream of zero-decision functions, which
# inflates the top-1% share without any decision behind it. That is why the s-expression
# languages (Clojure, Scheme, Racket, Elisp, Common Lisp, Hoon), the formats whose real code the
# grammar holds as one opaque string (Vue, Svelte, Astro, ColdFusion's cfscript, and a
# dollar-quoted PL/pgSQL body), and the grammars with no statement structure at all (Groovy,
# Prolog, COBOL paragraphs, Smalltalk's message-based control flow, assembly) all appear in
# EXT_LANG but not here. Their files and lines count; their complexity is not attributed.
# Verified against a snippet per language, not inferred from the node names.
_FUNC = {
    # --- the original set, unchanged ----------------------------------------------------
    "function_definition", "function_declaration", "function_item", "function_expression",
    "method_definition", "method_declaration", "method", "constructor_declaration",
    "arrow_function", "func_literal", "lambda", "singleton_method", "local_function_statement",
    # --- anonymous and first-class forms -------------------------------------------------
    "lambda_expression", "lambda_literal", "anonymous_function", "anonymous_function_expr",
    "anonymous_method_expression", "closure_expression", "fun_expression", "exp_lambda",
    "lambda_case", "function_literal", "closure", "block_argument", "anon_fun_expr",
    # --- named definitions, other spellings ----------------------------------------------
    "function", "function_statement", "function_signature",
    "procedure", "procedure_declaration", "subroutine",
    "subroutine_subprogram", "function_subprogram", "subprogram_body", "entry_body",
    "expression_function_declaration", "fun_decl", "fun_dec", "func_def", "funcdef",
    "function_or_value_defn", "member_defn", "method_or_prop_defn", "subroutine_declaration_statement",
    "let_binding", "rule", "monotonic_rule", "decl_def", "routine", "Decl", "def",
    "module_field_func", "defProc", "method_def", "value_declaration", "func_definition",
    "global_function", "storage_function", "native_function", "receive_function",
    # Dart puts `function_signature` and `function_body` side by side under the declaration with
    # no node spanning both, so the body is the only span that actually holds the decisions. In
    # Kotlin, Swift, D and Solidity the same name nests inside the definition, which makes the
    # definition a phantom and moves attribution from the definition to its body -- the same
    # partition either way, since decisions live in the body regardless.
    "function_body",
    # --- procedural blocks that are the real unit of behaviour in their language ---------
    "always_construct", "initial_construct", "task_declaration", "process_statement",
}

# The residue the two structural rules in _decisions_per_function cannot reach: a name that is
# the definition in one language and a NESTED, non-leaf component of it in another, where the
# outer node also holds decisions of its own so it is not a phantom either. Suppressing the name
# for those languages is the only way left to stop one empty function being opened per real one.
# Found by walking a snippet per language and looking for a function node inside a function node
# -- never inferred from the names, which is why the table is this short.
_FUNC_EXCLUDE = {
    "systemverilog": {"function", "function_statement"},
    "perl": {"function"},
    "jai": {"procedure"},
}

# Elixir has no syntax of its own for either a definition or a branch: `def`, `if` and `case` are
# all macros, so its grammar renders every one of them as an ordinary `call`. No node name in the
# tree means "function" or "if", and an Elixir module would otherwise be read as having no
# functions and no decisions at all -- a mainstream language scoring like an empty tree. Matching
# on the call's head word recovers both. Keyed by language, so no other grammar pays the lookup.
_CALL_HEADS = {
    "elixir": ({"def", "defp", "defmacro", "defmacrop"},
               {"if", "unless", "case", "cond", "for", "with", "try", "receive"}),
}

# Error handling is counted by keyword, which is the honest limit of this measurement and worth
# stating plainly: the patterns below are lexical, so they also fire inside comments and string
# literals, and they cannot see error handling a language expresses in its TYPES rather than its
# words -- Rust's `?`, Haskell's ExceptT, Elm's and Gleam's Result, an OCaml `option` return.
# Those languages are systematically undercounted here and no keyword list can fix it; only
# type-aware analysis could. The additions below extend the original C-family/English set to the
# languages added to EXT_LANG, and stop where a keyword would produce false positives: bare
# `error` and `exception` are excluded because they are ordinary identifiers in Go and ordinary
# prose in comments, and `warn` because it is logging in most codebases rather than handling.
_ERR = re.compile(
    rb"\b(try|catch|except|rescue|finally|throw|throws|raise|panic|recover|"
    rb"unwrap_or|map_err|expect_err|ok_or|"
    rb"assert|invariant|precondition|require|ensure|unreachable|"
    # Rust, Zig, Swift, Kotlin
    rb"rethrow|errdefer|bail|anyhow|with_context|runCatching|"
    # Perl, Tcl, Lua, R, Julia, MATLAB
    rb"croak|confess|carp|pcall|xpcall|tryCatch|stopifnot|withCallingHandlers|MException|"
    # Haskell, OCaml, F#, Erlang/Elixir, Clojure, Lisp
    rb"throwIO|catchError|catches|bracket|failwith|badmatch|ex-info|handler-case|ignore-errors|"
    # Objective-C, PowerShell, shells, Solidity, Fortran, PL/SQL, T-SQL
    rb"NSError|trap|revert|iostat|SQLERRM|RAISE_APPLICATION_ERROR|RAISERROR|die)\b|"
    rb"Write-Error|-ErrorAction|\bset\s+-[a-z]*e\b|\bON\s+SIZE\s+ERROR\b|\bWHEN\s+OTHERS\b|"
    rb"\berror\s+stop\b", re.I)
_ERR_TYPE = re.compile(rb"\bResult\s*<|\bEither\s*<|errors\.(New|Wrap|Is|As)\b|"
                       rb"fmt\.Errorf\b|\berr\s*!=\s*nil\b|"
                       # Scala and Rust spell their generics with brackets, not angles
                       rb"\b(Result|Either|Try)\s*\[|"
                       # the Erlang and Elixir error idiom is a tagged tuple, not a keyword
                       rb"\{:?error[,}]|"
                       # C's errno protocol
                       rb"\b(errno|perror|strerror)\b")

# --- second tier: code we can count but not parse -------------------------------------------
#
# These are real programming languages with no grammar in the pinned parser pack. They are
# counted as source -- they raise `source_files` and `prod_loc`, and `test_files` when they sit
# on a test path -- and they are never handed to a parser, so they contribute nothing to
# `decisions_gini_top1pct`. The subset is reported as `source_files_unparsed` so a reader can
# see how much of the tree the concentration figure was actually computed over.
#
# The reason this tier exists rather than the extensions simply being omitted: `source_files ==
# 0` and `prod_loc < 500` are hard no-buy gates. Omitting a language does not make its repository
# score badly, it makes it score zero by rule -- a measurement gap wearing the costume of a
# verdict. Counting without parsing is the honest middle: the volume is known, the structure
# is admitted to be unknown.
#
# Same exclusion as EXT_LANG: pure data, markup and config are absent, because a tree of nothing
# but JSON and YAML genuinely has no code and the gate should fire on it.
EXT_UNPARSED = {
    # ABAP, PL/I and the mainframe tail
    ".abap", ".pli", ".pl1", ".rexx", ".rex", ".jcl",
    # Apple and GNOME toolchains the pack has no grammar for
    ".applescript", ".vala", ".vapi", ".genie", ".metal",
    # statistics and numerics. `.do`/`.ado` are Stata, `.ijs` J, `.k`/`.q` the kdb+ family.
    # Mercury is knowingly absent: it shares `.m` with MATLAB and Objective-C, and losing a
    # MATLAB tree's attribution to disambiguate a rare language would be the worse trade.
    ".sas", ".do", ".ado", ".mata", ".ijs", ".apl", ".aplf", ".dyalog", ".k", ".q", ".gms",
    ".wl", ".wls",                       # Wolfram Language
    # ML-family and dependently typed languages outside the pack
    ".re", ".rei", ".dats", ".sats", ".curry", ".frege", ".mcr",
    # HPC and array languages
    ".chpl", ".fut", ".cuf", ".upc",
    # the object-oriented tail
    ".e", ".eiffel", ".m3", ".i3", ".ob2", ".obn", ".mod2", ".boo", ".cobra", ".ceylon",
    ".xtend", ".dylan", ".factor", ".io", ".wren", ".pike", ".icn", ".nial",
    # scripting the pack does not cover
    ".ahk", ".ahk2", ".coffee", ".litcoffee", ".ls", ".bal", ".rsc", ".moon", ".ring",
    ".sed", ".expect", ".4gl", ".p4",
    # hardware description outside the pack
    ".vams", ".sva", ".ucf", ".pcf", ".e2", ".asm51",
    # newer languages with no grammar yet
    ".mojopkg", ".carbon", ".val", ".vine", ".slint", ".gleam_ffi",
}

MAX_FILES = 2500
MAX_BYTES = 1_000_000

# --- the agentic second opinion --------------------------------------------------------------
#
# Two of the four no-buy gates are decided entirely by this file: `zero_code_files` on
# `source_files == 0` and `below_min_code_volume` on `prod_loc < 500`. Both are categorical -- a
# repository that fires either is rejected at any price -- and both are computed from a static
# table of file extensions. So the failure mode is specific and severe: a language absent from
# that table does not lower a score, it produces a confident rejection of a repository that may
# be full of code. No table is ever finished, which means the gate can always be fired by our own
# blind spot rather than by the repository.
#
# So when, and only when, the deterministic scan is about to fire one of those gates while
# looking at a tree that plainly holds substantial text it could not classify, a model is asked
# to look. It is a contradiction detector, not a measurement: nothing it returns is ever used as
# a number. If it contradicts the zero, the affected counts are reported as None -- UNMEASURED,
# never zero -- which is exactly what gate_no_buy() needs to stay silent, because it fires only
# on values that were actually measured. The deterministic figures are kept alongside under
# `*_deterministic` so nothing is lost and the disagreement is auditable.
#
# Both directions of the check are cheap relative to what they protect: the cost of a needless
# CLI call is seconds, and the cost of a wrong `zero_code_files` is a repository discarded.
AGENTIC_TIMEOUT = 240
# The shared default from models.py, not a cheaper model. This call looks like a yes/no about
# whether code exists, which is what justified a small model -- but its answer decides whether a
# hard no-buy gate fires, so a wrong yes/no discards a repository outright. One model per run,
# named in the output. A caller can still pass `model=` explicitly.
from models import DEFAULT_MODEL as AGENTIC_MODEL  # noqa: E402
AGENTIC_MIN_UNKNOWN_FILES = 3
# Roughly 60 lines of text. Set deliberately low: what these thresholds guard is a false
# `zero_code_files`, which is a claim about EXISTENCE, so very little code needs to be present
# before the claim is worth a second look. Higher floors were tried at 20KB and 5KB and both let a
# real three-file tree through to a confident rejection. The cost of erring low is bounded anyway,
# because the check also requires that a gate was about to fire -- which most trees never do.
AGENTIC_MIN_UNKNOWN_BYTES = 2_000

# Taken from the rubric rather than restated, because this is the threshold the check exists to
# defend: if the two ever drifted apart, the trigger would stop covering the gate it is meant to
# cover and nothing would fail loudly enough to notice.
try:
    from rubric import MIN_PROD_LOC as _GATE_PROD_LOC   # type: ignore
except Exception:                                       # standalone import, no rubric alongside
    _GATE_PROD_LOC = 500

# Extensions that are definitely not code, used only to decide whether an unrecognised file is
# worth telling the model about. Being wrong here costs a needless CLI call, never a measurement.
_NOT_CODE_EXT = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env",
    ".xml", ".xsd", ".xsl", ".dtd", ".csv", ".tsv", ".psv", ".parquet", ".avro",
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc", ".org", ".tex", ".bib",
    ".html", ".htm", ".xhtml", ".css", ".scss", ".sass", ".less", ".styl",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tiff", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".ogg", ".webm",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar", ".war", ".whl",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".lib", ".obj", ".pyc", ".pyo",
    ".class", ".wasm", ".db", ".sqlite", ".lock", ".sum", ".log", ".map", ".snap",
    ".proto", ".thrift", ".graphql", ".gql", ".prisma", ".capnp", ".smithy", ".avsc",
    ".tf", ".tfvars", ".tfstate", ".hcl", ".nix", ".bzl", ".bazel", ".cmake", ".gradle",
    ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore", ".npmrc", ".nvmrc",
    ".patch", ".diff", ".po", ".pot", ".mo", ".ipynb_checkpoints", ".ds_store",
    # Suffixes that shadow a real one rather than name a language. `metricbeat.yml.disabled` is
    # YAML; without these an archive of switched-off config reads as an unrecognised language and
    # buys a needless CLI call on a tree that genuinely has no code.
    ".disabled", ".bak", ".orig", ".rej", ".sample", ".example", ".template", ".dist",
    ".tmpl", ".old", ".save", ".swp", ".tmp",
}

_AGENTIC_PROMPT = """\
You are auditing one repository for a single factual question. Do not judge its quality.

A static scan of this tree recognised {found} source files and {loc} lines of production code,
which is low enough to classify the repository as containing no usable source code at all. The
scan works from a fixed table of file extensions, so it is blind to any language missing from
that table. It could not classify {unknown} files ({kb} KB), with these extensions: {exts}

Determine whether this repository actually contains first-party source code written by its
authors -- any programming language, however obscure. Vendored dependencies, build output,
generated code, data files, documentation and configuration do NOT count as source code.

Use Read, Grep, Glob and Bash to look. Be quick and concrete.

Reply with ONLY a JSON object, no prose and no code fences:
{{"has_source": true|false,
  "approx_source_files": <integer>,
  "approx_prod_loc": <integer>,
  "languages": ["..."],
  "evidence": "<one sentence, under 200 characters, naming what you found; no source code>"}}
"""


def _resolve_cli() -> tuple[str | None, bool]:
    """Find the CLI and decide whether the prompt must travel by stdin.

    Deliberately a local copy of the logic in material_census rather than an import: this module
    owns no part of that file, and a lane that measures the tree should not stop working because
    the model-assisted lane was refactored underneath it. The two Windows facts it encodes are
    real -- shutil.which honours PATHEXT while CreateProcess does not, so the resolved absolute
    path is what gets executed; and a .cmd/.bat shim reparses its command line, which a prompt
    containing quotes, braces and newlines cannot survive, so it goes down stdin instead.
    """
    if os.name == "nt":
        for candidate in ("claude.exe", "claude.com", "claude"):
            found = shutil.which(candidate)
            if found:
                return found, Path(found).suffix.lower() in (".cmd", ".bat")
        return None, False
    return shutil.which("claude") or None, False


def _agentic_has_source(repo: Path, found: int, loc: int, unknown: int, unknown_bytes: int,
                        exts: list[str], model: str, timeout: int) -> tuple[dict | None, str]:
    """Ask whether the tree holds source the extension table could not see.

    Returns (verdict, note). `verdict` is None whenever we did not get a usable answer, and the
    note always says why -- an unavailable second opinion must be distinguishable from a second
    opinion that agreed, or the mode field would be reporting our own silence as confirmation.
    """
    exe, prompt_on_stdin = _resolve_cli()
    if not exe:
        return None, "claude not on PATH; deterministic result stands unchallenged"
    prompt = _AGENTIC_PROMPT.format(
        found=found, loc=loc, unknown=unknown, kb=unknown_bytes // 1000,
        exts=", ".join(exts[:20]) or "none")
    cmd = [exe, "-p"]
    kwargs: dict = {
        "cwd": str(repo), "capture_output": True,
        # not text=True: the CLI emits UTF-8 and Windows would decode with a single-byte code page
        "encoding": "utf-8", "errors": "replace",
        # MODEL trust domain -- see childenv.py. Provider auth only.
        "timeout": max(1, timeout),
        "env": childenv.build_env(childenv.MODEL, provider="claude",
                                  passthrough=("HOME", "USERPROFILE", "XDG_CONFIG_HOME")),
    }
    if prompt_on_stdin:
        kwargs["input"] = prompt
    else:
        cmd.append(prompt)
        # Inheriting the parent's stdin is how one container in a fleet blocks forever on a
        # terminal that was never attached to it.
        kwargs["stdin"] = subprocess.DEVNULL
    # No Bash: this question is about which files hold source, which Read/Grep/Glob answer, and
    # `--add-dir` bounds those three and does not bound a shell. The isolation flags are what
    # stop a `.claude/settings.json` committed to the tree running its hooks here; a CLI that
    # cannot be isolated leaves the deterministic answer standing rather than being overruled by
    # a session the repository helped configure.
    try:
        isolation = childenv.isolation_flags("claude", exe)
    except childenv.ProviderNotIsolated:
        # Our own words, deliberately: this note is emitted, and the exception text names files
        # and flags that the leak audit would have to scrub.
        return None, ("this CLI cannot be isolated from configuration supplied by the tree; "
                      "the deterministic result stands unchallenged")
    cmd += ["--output-format", "json", "--add-dir", str(repo), "--model", model,
            "--allowedTools", "Read Grep Glob", *isolation]
    try:
        p = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return None, f"the second opinion did not return within {timeout}s"
    except OSError as e:
        return None, f"could not run claude ({type(e).__name__})"
    if p.returncode != 0:
        return None, f"claude exited {p.returncode}"
    payload = _extract_json(p.stdout or "")
    if not isinstance(payload, dict) or "has_source" not in payload:
        return None, "claude returned no parseable verdict"
    return payload, ""


def _extract_json(stdout: str) -> dict | None:
    """`claude -p --output-format json` wraps the answer; the answer may itself be fenced."""
    text = stdout
    try:
        outer = json.loads(stdout)
        inner = outer.get("result") if isinstance(outer, dict) else None
        if isinstance(inner, dict):
            return inner
        if isinstance(inner, str):
            text = inner
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _gini_top(values: list[int], frac: float = 0.01) -> float:
    """Share of all decision points held by the densest `frac` of functions."""
    if not values:
        return 0.0
    total = sum(values)
    if total == 0:
        return 0.0
    k = max(1, round(len(values) * frac))
    return sum(sorted(values, reverse=True)[:k]) / total


def _decisions_per_function(tree, data_len: int, lang: str = "") -> list[int]:
    """One count per function. Iterative walk -- deep trees would blow a recursive one.

    Two corrections keep the distribution honest across grammars that spell the same construct
    more than once. Both were found by walking snippets in every mapped language and looking for
    a function node inside a function node.

      * A leaf node is never a function. Many grammars expose the KEYWORD as a named node --
        Python's `def`, PHP's and Lua's `function`, Ada's `procedure` -- and those names have to
        stay in _FUNC because in Lean, Haskell and Odin the same spelling IS the definition.
      * A wrapper that scores zero only because a nested function took all of its decisions is a
        phantom, not a function: Fortran's `subroutine` around its own body, Odin's
        `procedure_declaration` around its `procedure`. Counting it would add an empty function
        per real function, which halves every density and moves the top-1% share for a reason
        that is grammar bookkeeping rather than code. A function with genuinely zero decisions
        and no nested function is real, and is kept -- straight-line code is a true data point.
    """
    counts: dict[int, int] = {}
    wraps: set[int] = set()               # function keys that contain another counted function
    func = _FUNC - _FUNC_EXCLUDE[lang] if lang in _FUNC_EXCLUDE else _FUNC
    heads = _CALL_HEADS.get(lang)
    stack = [(tree.root_node, None)]
    while stack:
        node, fn = stack.pop()
        t = node.type
        if heads and t == "call" and node.child_count:
            first = node.children[0]
            if not first.child_count:
                word = (first.text or b"").decode("utf-8", "replace")
                if word in heads[0]:
                    t = "function_definition"      # an Elixir `def` call IS the definition
                elif word in heads[1]:
                    t = "if_statement"             # ... and its `if`/`case` call IS the branch
        if t in func and node.child_count:
            if fn is not None:
                wraps.add(fn)
            fn = node.start_byte * data_len.bit_length() + node.end_byte  # stable per-node key
            counts.setdefault(fn, 0)
        elif fn is not None and t in _DECISION:
            counts[fn] = counts.get(fn, 0) + 1
        for ch in node.children:
            stack.append((ch, fn))
    return [n for k, n in counts.items() if n or k not in wraps]


def collect(repo: Path, allow_agentic: bool = True, model: str = AGENTIC_MODEL,
            agentic_timeout: int = AGENTIC_TIMEOUT) -> dict:
    """Measure the tree. `allow_agentic` gates the second opinion described above; the
    RQE_NO_AGENTIC environment variable turns it off for a whole fleet without a code change."""
    out: dict = {"probe": "code_structure", "ok": False}
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore
    except Exception as e:
        out["error"] = (f"parser unavailable ({type(e).__name__}) -- install "
                        f"tree-sitter-language-pack; structure criteria reported unscored")
        return out

    prod_loc = err_hits = n_files = n_parsed = n_parse_failed = n_unparsed = 0
    test_files = 0
    skipped_big = skipped_generated = 0
    capped = False
    per_func: list[int] = []
    parsers: dict[str, object] = {}
    # Files we neither recognised nor could rule out as data. The only consumer is the trigger for
    # the second opinion: a zero source count is only suspicious if there was something to miss.
    unknown_files = unknown_bytes = 0
    unknown_exts: dict[str, int] = {}
    # Languages whose grammar could not be obtained are remembered, because get_parser may reach
    # the network for a grammar it does not have cached. Without this, an offline run would pay
    # that failure once per FILE instead of once per language.
    parser_unavailable: set[str] = set()

    try:
        for p in repo.rglob("*"):
            if n_files >= MAX_FILES:
                capped = True
                break
            if not p.is_file() or p.is_symlink():
                continue
            # as_posix, not str: rglob yields backslash separators on Windows, and every skip
            # and test regex below anchors on `/`. Matching on the native separator would make
            # those patterns silently never fire there -- vendored trees would be counted as
            # first-party and test files as production code, on that platform only.
            rel = p.relative_to(repo).as_posix()
            if _SKIP_DIR.search(rel) or _SKIP_FILE.search(rel):
                continue
            ext = p.suffix.lower()
            lang = EXT_LANG.get(ext)
            unparsed_tier = lang is None and ext in EXT_UNPARSED
            if lang is None and not unparsed_tier:
                # Unrecognised. Note it if it could plausibly have been code, so that a zero
                # source count can be told apart from a zero source count with 400 unexplained
                # files sitting next to it. Extensionless files are skipped here rather than
                # counted: LICENSE, Makefile and Dockerfile would otherwise make every tree look
                # suspicious. Binary sniffing is cheap because only the first block is read.
                sfx = [s.lower() for s in p.suffixes[-2:]]
                if (ext and unknown_files < 500
                        and not any(s in _NOT_CODE_EXT for s in sfx)):
                    try:
                        if 0 < p.stat().st_size <= MAX_BYTES:
                            with p.open("rb") as fh:
                                head = fh.read(4096)
                            if head and b"\x00" not in head:
                                unknown_files += 1
                                unknown_bytes += p.stat().st_size
                                unknown_exts[ext] = unknown_exts.get(ext, 0) + 1
                    except OSError:
                        pass
                continue
            # Test files are counted and then excluded from every other measurement. The count
            # exists because "this repository has no tests at all" must be answerable without
            # running a build -- the build lane can fail for the runner's reasons, and a gate
            # that depends on it would then fire on a repository that does have tests.
            if _TEST.search(rel):
                test_files += 1
                continue
            try:
                if p.stat().st_size > MAX_BYTES:
                    skipped_big += 1
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if ext == ".ipynb":
                data = _notebook_source(data)
                if not data.strip():
                    continue          # a notebook with no code cells is prose, not code
            elif _GENERATED_HDR.search(data[:4096]):
                skipped_generated += 1
                continue

            n_files += 1
            prod_loc += data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
            err_hits += len(_ERR.findall(data)) + len(_ERR_TYPE.findall(data))
            if unparsed_tier:
                n_unparsed += 1
                continue              # counted as code; no grammar exists to attribute it

            if ext in _AMBIGUOUS:
                lang = _AMBIGUOUS[ext](data)
            if lang in parser_unavailable:
                n_parse_failed += 1
                continue
            try:
                if lang not in parsers:
                    parsers[lang] = get_parser(lang)
            except Exception:
                parser_unavailable.add(lang)   # grammar missing on this machine, not in this file
                n_parse_failed += 1
                continue
            try:
                tree = parsers[lang].parse(data)  # type: ignore[attr-defined]
                per_func.extend(_decisions_per_function(tree, len(data), lang))
                n_parsed += 1
            except Exception:
                n_parse_failed += 1
    except OSError as e:
        out["error"] = f"could not walk tree: {type(e).__name__}"
        return out

    kloc = max(prod_loc / 1000.0, 0.001)
    out.update({
        "prod_loc": prod_loc,
        "source_files": n_files,
        "test_files": test_files,
        "n_functions_seen": len(per_func),
        "n_files_parsed": n_parsed,
        "n_files_parse_failed": n_parse_failed,
        # Counted as code, never handed to a grammar. `source_files` includes these; the pair
        # (source_files, source_files_unparsed) says how much of the tree the concentration
        # figure was computed over, so a low gini on a mostly-unparsed tree is readable as thin
        # evidence rather than as flat complexity.
        "source_files_unparsed": n_unparsed,
        "decisions_gini_top1pct": round(_gini_top(per_func), 4) if per_func else None,
        "error_handling_per_kloc": round(err_hits / kloc, 3) if n_files else None,
        "ok": True,
    })

    # Bounds are reported, never applied silently -- a truncated scan must not look complete.
    notes = []
    if capped:
        notes.append(f"file cap {MAX_FILES} reached; covers the first {n_files} source files")
    if skipped_big:
        notes.append(f"{skipped_big} files over {MAX_BYTES // 1000}KB skipped")
    if skipped_generated:
        notes.append(f"{skipped_generated} generated files skipped")
    if n_parse_failed:
        notes.append(f"{n_parse_failed} files failed to parse")
    if notes:
        out["bounds"] = "; ".join(notes)
    if not per_func:
        out["note"] = ("no functions parsed; concentration criterion unscored rather than zero")

    # --- the second opinion, and the only place a measured value is ever withdrawn ------------
    #
    # The trigger is deliberately narrow. It requires BOTH that the deterministic scan is about
    # to fire a categorical gate AND that the tree held enough unclassified text for a blind spot
    # to be a plausible explanation. A repository of nothing but YAML satisfies the first and not
    # the second, which is why the config-only tree still fires its gate without a CLI call.
    out["structure_probe_mode"] = "deterministic"
    would_gate = n_files == 0 or prod_loc < _GATE_PROD_LOC
    substantial_unknown = (unknown_files >= AGENTIC_MIN_UNKNOWN_FILES
                           and unknown_bytes >= AGENTIC_MIN_UNKNOWN_BYTES)
    if allow_agentic and would_gate and substantial_unknown and not os.environ.get("RQE_NO_AGENTIC"):
        top = sorted(unknown_exts, key=lambda e: -unknown_exts[e])
        out["structure_unknown_files"] = unknown_files
        out["structure_unknown_exts"] = ", ".join(f"{e}({unknown_exts[e]})" for e in top[:10])
        verdict, why = _agentic_has_source(repo, n_files, prod_loc, unknown_files, unknown_bytes,
                                          top, model, agentic_timeout)
        if verdict is None:
            # An unavailable second opinion changes nothing. The deterministic result stands and
            # says so, because reporting silence as agreement is how a blind spot becomes a fact.
            out["structure_probe_mode"] = "agentic_unavailable"
            out["structure_agentic_note"] = why
        elif verdict.get("has_source"):
            # Contradiction. The counts the gates read are withdrawn to None -- UNMEASURED, not
            # zero -- so gate_no_buy() stays silent on them and the rubric reports the structure
            # criteria unscored, which is the truthful state: we know there is code and we do not
            # know how much. The model's own numbers are NOT substituted; they are an estimate
            # from a tool that was asked a yes/no question, and putting them in a field the rubric
            # bands on would be inventing a measurement to replace one we admitted we lack.
            out["structure_probe_mode"] = "agentic_contradicted"
            out["source_files_deterministic"] = n_files
            out["prod_loc_deterministic"] = prod_loc
            out["test_files_deterministic"] = test_files
            out["source_files"] = None
            out["prod_loc"] = None
            out["test_files"] = None
            out["error_handling_per_kloc"] = None
            out["structure_agentic_languages"] = ", ".join(
                str(x) for x in (verdict.get("languages") or [])[:8])
            out["structure_agentic_note"] = (
                "the extension table recognised no usable source but a second opinion found "
                "first-party code, so the counts the no-buy gates read are reported unmeasured "
                "rather than zero: " + " ".join(str(verdict.get("evidence") or "").split())[:200])
        else:
            # Agreement. The gate still fires, now on two independent readings rather than one.
            out["structure_probe_mode"] = "agentic_confirmed"
            out["structure_agentic_note"] = (
                "a second opinion agreed the tree holds no first-party source: "
                + " ".join(str(verdict.get("evidence") or "").split())[:200])
    return out
