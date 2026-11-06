"""check-tests: keep a test suite honest. Tests come first, are never bypassed, and are fast.

One file, standard library only, so it can be copied into any repository and run
in CI on every pull request:

    python check_tests.py scan .                 no skipped, faked, swallowed or hidden tests
    python check_tests.py durations junit.xml    every test under 200 ms (target 50 ms)
    python check_tests.py test-first --base origin/main
        the new tests fail on the base branch's code (they test something the
        change adds) and the whole suite passes on the pull request's code;
        no test is deleted or loses assertions unless a maintainer wrote the change

Make all three required checks, and a pull request cannot merge until they pass.
They matter most when AI agents write code: an agent under pressure to make a
build green is tempted to skip a test, assert True, or catch the failure.

The checks report problems; they never change a result. A failing test is fixed
by finding and fixing the root cause in the code under test, never by editing the
test to pass, skipping it, or changing how it is collected or reported.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SLOW_LIMIT = 0.200   # seconds: a single test slower than this fails the build
FAST_TARGET = 0.050  # seconds: a single test slower than this is reported as a warning

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist",
             "build", ".mypy_cache", ".ruff_cache"}

# Pytest hooks that can rewrite, hide or reorder test outcomes. A project that
# needs one adds it through a pull request a maintainer writes and reviews.
OUTCOME_HOOKS = {
    "pytest_runtest_makereport", "pytest_runtest_call", "pytest_pyfunc_call",
    "pytest_runtest_protocol", "pytest_report_teststatus", "pytest_collection_modifyitems",
    "pytest_deselected", "pytest_ignore_collect", "pytest_collect_file",
    "pytest_sessionfinish", "pytest_terminal_summary", "pytest_runtest_teardown",
    "pytest_runtest_setup", "pytest_configure",
}

SKIP_DECORATORS = re.compile(
    r"^(pytest\.mark\.(skip|skipif|xfail)|unittest\.(skip|skipIf|skipUnless|expectedFailure)|"
    r"(skip|skipIf|skipUnless|expectedFailure))$"
)
SKIP_CALLS = {"pytest.skip", "pytest.xfail", "pytest.importorskip", "self.skipTest",
              "unittest.SkipTest", "pytest.exit"}
EXIT_CALLS = {"sys.exit", "os._exit", "exit", "quit", "os.abort"}
FAKE_PASS_VALUES = {"pass", "passed", "ok", "success", "true", "1"}

JS_RULES = [
    ("skip", re.compile(r"\b(?:it|test|describe|suite|context)\s*\.\s*(?:skip|todo)\s*\(")),
    ("skip", re.compile(r"\bx(?:it|test|describe)\s*\(")),
    ("skip", re.compile(r"\{\s*(?:skip|todo)\s*:\s*(?:true|['\"])")),
    ("only", re.compile(r"\b(?:it|test|describe|suite|context)\s*\.\s*only\s*\(")),
    ("only", re.compile(r"\bf(?:it|describe)\s*\(")),
    ("trivial-assert", re.compile(
        r"\bexpect\s*\(\s*(?:true|1|!0|(['\"`])[^'\"`]*\1)\s*\)\s*\.\s*"
        r"(?:toBe|toEqual|toStrictEqual|toBeTruthy|toBeDefined)\s*\(\s*(?:true|1|!0|(['\"`])[^'\"`]*\2)?\s*\)")),
    ("trivial-assert", re.compile(r"\bassert(?:\.ok)?\s*\(\s*(?:true|1|!0)\s*[,)]")),
    ("trivial-assert", re.compile(r"\bassert\.(?:equal|strictEqual|deepEqual)\s*\(\s*(true|1)\s*,\s*\1\s*[,)]")),
    ("exit", re.compile(r"\bprocess\s*\.\s*exit\s*\(")),
    ("swallowed", re.compile(r"catch\s*(?:\(\s*\w*\s*\))?\s*\{\s*\}")),
]

# Ways to weaken a test run from a workflow, a test script or the test config.
RUN_RULES = [
    ("ignored-failure", re.compile(r"\|\|\s*(?:true|:|exit\s+0)\b")),
    ("ignored-failure", re.compile(r"continue-on-error\s*:\s*true")),
    ("ignored-failure", re.compile(r"(?m)^\s*set\s+\+e\b")),
    ("ignored-failure", re.compile(r"--exit-zero\b")),
    ("ignored-failure", re.compile(r"--passWithNoTests\b")),
    ("ignored-failure", re.compile(r"--continue-on-collection-errors\b")),
    ("deselected", re.compile(r"--deselect\b")),
    ("deselected", re.compile(r"(?<![\w-])-k\s+['\"]?\s*not\b")),
    ("deselected", re.compile(r"(?<![\w-])--ignore(?:-glob)?\b")),
    ("deselected", re.compile(r"(?<![\w-])-p\s+no:")),
    ("deselected", re.compile(r"(?<![\w-])--testPathIgnorePatterns\b")),
    ("skip", re.compile(r"(?<![\w-])-m\s+['\"]?\s*not\b")),
    ("skip", re.compile(r"\bxfail_strict\s*=\s*(?:false|0)\b", re.IGNORECASE)),
]

CONFIG_FILES = {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "package.json",
                "vitest.config.ts", "vitest.config.js", "jest.config.js", "jest.config.ts"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


# ---------------------------------------------------------------------------
# Which files are tests
# ---------------------------------------------------------------------------


def is_python_test(path: str) -> bool:
    name = Path(path).name
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
        or "tests" in Path(path).parts)


def is_js_test(path: str) -> bool:
    return bool(re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", Path(path).name)) or (
        "tests" in Path(path).parts and bool(re.search(r"\.[cm]?[jt]sx?$", path)))


def is_test_file(path: str) -> bool:
    return is_python_test(path) or is_js_test(path)


def is_workflow(path: str) -> bool:
    parts = Path(path).parts
    return len(parts) >= 3 and parts[-3:-1] == (".github", "workflows") and path.endswith(
        (".yml", ".yaml"))


# ---------------------------------------------------------------------------
# Python tests
# ---------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _is_constant_truthy(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value)


def _same_expr(a: ast.AST, b: ast.AST) -> bool:
    return ast.dump(a) == ast.dump(b)


def _is_test_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def _checks_something(fn: ast.AST) -> bool:
    """True if the test asserts, expects an exception, or calls an assert/expect helper."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if _dotted(item.context_expr) in {"pytest.raises", "pytest.warns", "self.assertRaises",
                                                  "self.assertRaisesRegex", "self.assertWarns",
                                                  "pytest.deprecated_call"}:
                    return True
        if isinstance(node, ast.Call):
            name = _dotted(node.func).rsplit(".", 1)[-1].lower()
            if name.startswith(("assert", "expect", "verify", "check_")) or name == "fail":
                return True
    return False


def _body_is_empty(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # docstring
    return all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
               or (isinstance(s, ast.Return) and s.value is None) for s in body)


def _handler_catches_failures(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = [handler.type] if not isinstance(handler.type, ast.Tuple) else handler.type.elts
    return any(_dotted(n) in {"AssertionError", "Exception", "BaseException", "pytest.fail.Exception",
                              "Failed"} for n in names)


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler)) or any(
        isinstance(n, ast.Call) and _dotted(n.func) in {"pytest.fail", "self.fail"}
        for n in ast.walk(handler))


def python_findings(path: str, source: str) -> list[Finding]:
    """Every way the Python file at `path` skips, fakes or hides a test result."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        return [Finding(path, e.lineno or 1, "syntax", f"does not parse: {e.msg}")]
    out: list[Finding] = []

    def add(node: ast.AST, rule: str, message: str) -> None:
        out.append(Finding(path, getattr(node, "lineno", 1), rule, message))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                if SKIP_DECORATORS.match(_dotted(dec)):
                    add(dec, "skip", f"'{node.name}' is skipped or expected to fail "
                                     f"(@{_dotted(dec)}); fix the cause instead")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in OUTCOME_HOOKS:
            add(node, "hook", f"'{node.name}' can change how tests are collected or reported; "
                              "only a maintainer may add it")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _dotted(target) == "pytestmark" and any(
                        SKIP_DECORATORS.match(_dotted(v)) for v in ast.walk(node.value)
                        if isinstance(v, (ast.Attribute, ast.Call))):
                    add(node, "skip", "pytestmark skips or xfails the whole module")
                if _dotted(target).startswith(("pytest.", "unittest.", "builtins.")):
                    add(node, "hook", f"reassigns {_dotted(target)}, which changes how tests run")
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in SKIP_CALLS:
                add(node, "skip", f"calls {name}(); a test must run and pass, not skip")
            elif name in EXIT_CALLS:
                add(node, "exit", f"calls {name}(), which can end the run with a false result")
            elif name.endswith(("monkeypatch.setattr", "mock.patch", "patch.object", "patch")) and any(
                    isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and re.match(r"^(pytest|_pytest|unittest)\b", a.value) for a in node.args):
                add(node, "hook", "patches the test framework itself")
        if isinstance(node, ast.Assert):
            if _is_constant_truthy(node.test):
                add(node, "trivial-assert", "asserts a constant, which always passes")
            elif isinstance(node.test, ast.Compare) and len(node.test.comparators) == 1 and \
                    isinstance(node.test.ops[0], (ast.Eq, ast.Is, ast.GtE, ast.LtE)) and \
                    _same_expr(node.test.left, node.test.comparators[0]):
                add(node, "trivial-assert", "compares a value with itself, which always passes")

    for fn in [n for n in ast.walk(tree) if _is_test_function(n)]:
        if _body_is_empty(fn):
            add(fn, "empty", f"'{fn.name}' has no body; write the test or remove the stub")
            continue
        if not _checks_something(fn):
            add(fn, "no-assert", f"'{fn.name}' checks nothing: add an assert or pytest.raises")
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and \
                    str(node.value.value).strip().lower() in FAKE_PASS_VALUES:
                add(node, "fake-pass", f"'{fn.name}' returns {node.value.value!r} instead of asserting")
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    if _handler_catches_failures(handler) and not _reraises(handler):
                        add(handler, "swallowed", f"'{fn.name}' catches failures without re-raising; "
                                                  "use pytest.raises for expected errors")
    return out


def test_names(path: str, source: str) -> dict[str, int]:
    """{test id: number of checks in it} for a Python test file ({} if it does not parse)."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return {}
    names: dict[str, int] = {}

    def count(fn: ast.AST) -> int:
        n = 0
        # A `with pytest.raises(...)` is one check: count the with, not the call inside it.
        managers = {id(i.context_expr) for node in ast.walk(fn)
                    if isinstance(node, (ast.With, ast.AsyncWith)) for i in node.items}
        for node in ast.walk(fn):
            if isinstance(node, ast.Assert):
                n += 1
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                n += sum(1 for i in node.items
                         if _dotted(i.context_expr).rsplit(".", 1)[-1].lower().startswith(
                             ("raises", "warns", "assert", "deprecated_call")))
            elif isinstance(node, ast.Call) and id(node) not in managers and \
                    _dotted(node.func).rsplit(".", 1)[-1].lower().startswith(("assert", "expect", "raises")):
                n += 1
        return n

    for node in tree.body:
        if _is_test_function(node):
            names[f"{path}::{node.name}"] = count(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if _is_test_function(item):
                    names[f"{path}::{node.name}::{item.name}"] = count(item)
    return names


# ---------------------------------------------------------------------------
# JavaScript / TypeScript tests, workflows and test configuration
# ---------------------------------------------------------------------------


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _strip_js_comments(text: str) -> str:
    # Keeps line numbers: comments are replaced by the same number of newlines.
    return re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)


def js_findings(path: str, source: str) -> list[Finding]:
    text = _strip_js_comments(source)
    messages = {
        "skip": "skips a test; a test must run and pass",
        "only": "runs only some tests, so the others are silently left out",
        "trivial-assert": "asserts a constant, which always passes",
        "exit": "exits the process, which can end the run with a false result",
        "swallowed": "an empty catch block hides failures",
    }
    return [Finding(path, _line_of(text, m.start()), rule, messages[rule])
            for rule, rx in JS_RULES for m in rx.finditer(text)]


def run_config_findings(path: str, text: str) -> list[Finding]:
    """Workflows, test scripts and test config that ignore failures or leave tests out."""
    out = []
    for rule, rx in RUN_RULES:
        for m in rx.finditer(text):
            line_text = text.splitlines()[_line_of(text, m.start()) - 1].strip()
            if line_text.startswith("#"):
                continue
            out.append(Finding(path, _line_of(text, m.start()), rule,
                               f"'{m.group(0).strip()}' lets failing or left-out tests go unnoticed"))
    return out


def file_findings(path: str, text: str) -> list[Finding]:
    name = Path(path).name
    if path.endswith(".py") and is_python_test(path):
        return python_findings(path, text)
    if is_js_test(path):
        return js_findings(path, text)
    if is_workflow(path) or name in CONFIG_FILES:
        return run_config_findings(path, text)
    if path.endswith(".py"):
        # Pytest plugins and hooks can live outside tests/ too.
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        return [Finding(path, n.lineno, "hook", f"'{n.name}' can change how tests are reported; "
                                                "only a maintainer may add it")
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in OUTCOME_HOOKS]
    return []


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            yield Path(dirpath, name)


def scan(root: Path, allow: set[str] = frozenset()) -> list[Finding]:
    """Findings for every test, workflow and test-config file under `root`.

    `allow` lists repo-relative files a maintainer has reviewed (for example the
    test policy's own tests, which contain the patterns they check for).
    """
    out = []
    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in allow:
            continue
        if not (is_test_file(rel) or is_workflow(rel) or path.name in CONFIG_FILES or rel.endswith(".py")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out += file_findings(rel, text)
    return out


# ---------------------------------------------------------------------------
# Test results (JUnit XML, written by pytest --junitxml and node --test-reporter=junit)
# ---------------------------------------------------------------------------


@dataclass
class Case:
    name: str
    seconds: float
    outcome: str  # passed, failed, error, skipped
    message: str = ""


@dataclass
class Results:
    cases: list[Case] = field(default_factory=list)
    collection_errors: int = 0

    @property
    def failed(self) -> list[Case]:
        return [c for c in self.cases if c.outcome in ("failed", "error")]

    @property
    def skipped(self) -> list[Case]:
        return [c for c in self.cases if c.outcome == "skipped"]

    @property
    def passed(self) -> list[Case]:
        return [c for c in self.cases if c.outcome == "passed"]

    @property
    def all_passed(self) -> bool:
        return bool(self.cases) and not self.failed and not self.skipped and not self.collection_errors


def parse_junit(xml_text: str) -> Results:
    root = ET.fromstring(xml_text)
    results = Results()
    for case in root.iter("testcase"):
        name = "::".join(p for p in (case.get("classname", ""), case.get("name", "")) if p)
        seconds = float(case.get("time") or 0)
        outcome, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            child = case.find(tag)
            if child is not None:
                outcome = {"failure": "failed"}.get(tag, tag)
                message = (child.get("message") or child.text or "").strip()
                break
        results.cases.append(Case(name, seconds, outcome, message))
    for suite in root.iter("testsuite"):
        # pytest records a module that fails to import as an error case; some
        # runners only count it on the suite.
        reported = int(suite.get("errors") or 0)
        counted = sum(1 for c in suite.iter("testcase") if c.find("error") is not None)
        results.collection_errors += max(0, reported - counted)
    return results


def duration_findings(results: Results, limit: float = SLOW_LIMIT,
                      target: float = FAST_TARGET) -> tuple[list[str], list[str]]:
    """(errors for tests slower than `limit`, warnings for tests slower than `target`)."""
    errors = [f"{c.name} took {c.seconds * 1000:.0f} ms (limit {limit * 1000:.0f} ms)"
              for c in results.cases if c.seconds > limit]
    warnings = [f"{c.name} took {c.seconds * 1000:.0f} ms (target {target * 1000:.0f} ms)"
                for c in results.cases if target < c.seconds <= limit]
    return errors, warnings


def result_errors(results: Results) -> list[str]:
    """Everything short of 'every test ran and passed'."""
    errors = []
    if not results.cases:
        errors.append("no tests ran")
    errors += [f"{c.name} {c.outcome}: {c.message[:300]}" for c in results.failed]
    errors += [f"{c.name} was skipped: {c.message[:200]}" for c in results.skipped]
    if results.collection_errors:
        errors.append(f"{results.collection_errors} test file(s) failed to load")
    return errors


# ---------------------------------------------------------------------------
# Tests first
# ---------------------------------------------------------------------------

DOC_SUFFIXES = (".md", ".txt", ".rst", ".png", ".jpg", ".svg", ".gif")


def code_changes(changed: list[str]) -> tuple[list[str], list[str]]:
    """(changed test files, changed non-test code files); docs and images are neither."""
    tests = [p for p in changed if is_test_file(p) and Path(p).name != "conftest.py"]
    code = [p for p in changed if p not in tests and not p.endswith(DOC_SUFFIXES)
            and not p.startswith((".github/", "tools/")) and Path(p).name != "conftest.py"]
    return tests, code


def weakened_tests(base: dict[str, int], head: dict[str, int]) -> list[str]:
    """Tests that were removed or now check fewer things than on the base branch."""
    out = [f"{name} was removed" for name in base if name not in head]
    out += [f"{name} now has {head[name]} check(s), down from {n}"
            for name, n in base.items() if name in head and head[name] < n]
    return out


def tdd_errors(changed: list[str], card_type: str, red: Results | None, green: Results,
               weakened: list[str], maintainer: bool) -> list[str]:
    """The tests-first verdict for one pull request.

    changed     files the pull request changes
    card_type   the card's type: feature, bug, chore or refactor
    red         the pull request's changed test files run against the base branch's
                code (None when no test file changed)
    green       the whole suite run on the pull request's code
    weakened    from weakened_tests()
    maintainer  the author is a maintainer (a person), not an automated contributor
    """
    errors = []
    tests, code = code_changes(changed)
    if code and not tests and card_type != "refactor":
        errors.append(
            "code changed but no test did: write the failing test first "
            f"(changed: {', '.join(code[:5])})")
    # A change with code must prove itself with tests that fail without it. A
    # tests-only change (more tests for existing behaviour) only has to pass.
    if tests and code and card_type != "refactor":
        if red is None or not red.cases and not red.collection_errors:
            errors.append("the new tests were not run against the base branch")
        elif not red.failed and not red.collection_errors:
            errors.append(
                "the new or changed tests already pass without this change, so they do not "
                "test it: write a test that fails until the feature or fix is in place")
    if card_type == "refactor" and tests and not maintainer:
        errors.append("a refactor must not change tests: the existing tests prove behaviour is kept")
    if weakened and not maintainer:
        errors += [f"{w}: only a maintainer may remove or weaken a test" for w in weakened]
    errors += result_errors(green)
    return errors


# ---------------------------------------------------------------------------
# Command line (used by CI in each product repository)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _test_env() -> dict[str, str]:
    """A minimal environment, so code under test never sees CI secrets."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "PYTHONPATH", "VIRTUAL_ENV")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_pytest(cwd: Path, paths: list[str], junit: Path, timeout: int = 600) -> Results:
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", *paths]
    env = _test_env()
    src = cwd / "src"
    if src.is_dir():
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(src), env.get("PYTHONPATH", "")) if p)
    try:
        subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Results([Case("pytest", float(timeout), "error", "the test run timed out")])
    if not junit.exists():
        return Results([Case("pytest", 0.0, "error", "pytest wrote no results")])
    return parse_junit(junit.read_text(encoding="utf-8"))


def _tests_by_name(root: Path, files: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for rel in files:
        path = root / rel
        if path.is_file() and rel.endswith(".py"):
            out.update(test_names(rel, path.read_text(encoding="utf-8")))
    return out


def cmd_test_first(base: str, card_type: str, maintainer: bool, root: Path) -> int:
    changed = [p for p in _git("diff", "--name-only", "--diff-filter=ACMRD", f"{base}...HEAD",
                               cwd=root).splitlines() if p]
    tests, _ = code_changes(changed)
    base_test_files = [p for p in _git("ls-tree", "-r", "--name-only", base, cwd=root).splitlines()
                       if is_python_test(p) and Path(p).name != "conftest.py"]
    work = Path(tempfile.mkdtemp(prefix="test-first-"))
    red = None
    try:
        worktree = work / "base"
        _git("worktree", "add", "--detach", str(worktree), base, cwd=root)
        try:
            base_names = _tests_by_name(worktree, base_test_files)
            existing = [t for t in tests if (root / t).is_file()]
            for rel in existing:
                target = worktree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, target)
            if existing:
                red = run_pytest(worktree, existing, work / "red.xml")
        finally:
            _git("worktree", "remove", "--force", str(worktree), cwd=root)
        head_test_files = [p for p in _git("ls-files", cwd=root).splitlines()
                           if is_python_test(p) and Path(p).name != "conftest.py"]
        head_names = _tests_by_name(root, head_test_files)
        green = run_pytest(root, [], work / "green.xml")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    errors = tdd_errors(changed, card_type, red, green, weakened_tests(base_names, head_names),
                        maintainer)
    if red is not None:
        print(f"Base branch + new tests: {len(red.failed)} failing, {len(red.passed)} passing "
              "(the new tests must fail here)")
    print(f"This branch: {len(green.passed)} passed, {len(green.failed)} failed, "
          f"{len(green.skipped)} skipped")
    for e in errors:
        print(f"ERROR: {e}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan", help="find skipped, faked or hidden tests")
    s.add_argument("root", nargs="?", default=".")
    s.add_argument("--allow", action="append", default=[],
                   help="a repo-relative file a maintainer has reviewed (repeatable)")
    d = sub.add_parser("durations", help="fail tests slower than the limit")
    d.add_argument("junit")
    d.add_argument("--limit-ms", type=float, default=SLOW_LIMIT * 1000)
    d.add_argument("--target-ms", type=float, default=FAST_TARGET * 1000)
    t = sub.add_parser("test-first", help="new tests fail on the base branch, all pass here")
    t.add_argument("--base", default="origin/main")
    t.add_argument("--card-type", default="feature", choices=("feature", "bug", "chore", "refactor"))
    t.add_argument("--maintainer", action="store_true",
                   help="the author is a maintainer; CI sets this from the PR author")
    args = parser.parse_args(argv)

    if args.command == "scan":
        findings = scan(Path(args.root), set(args.allow))
        for f in findings:
            print(f"ERROR: {f}")
        print(f"{len(findings)} test integrity problem(s)")
        return 1 if findings else 0
    if args.command == "durations":
        results = parse_junit(Path(args.junit).read_text(encoding="utf-8"))
        errors, warnings = duration_findings(results, args.limit_ms / 1000, args.target_ms / 1000)
        for w in warnings:
            print(f"WARNING: {w}")
        for e in errors:
            print(f"ERROR: {e}")
        print(f"{len(results.cases)} tests; {len(errors)} over the limit, {len(warnings)} over target")
        return 1 if errors or not results.cases else 0
    return cmd_test_first(args.base, args.card_type, args.maintainer, Path.cwd())


if __name__ == "__main__":
    sys.exit(main())
