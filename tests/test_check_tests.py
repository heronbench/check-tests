"""The test integrity rules: no bypassed tests, fast tests, tests first (check_tests.py)."""
import pytest

import check_tests as tp


def rules(source: str, path: str = "tests/test_x.py") -> set[str]:
    return {f.rule for f in tp.python_findings(path, source)}


# ---------------------------------------------------------------------------
# Python bypasses
# ---------------------------------------------------------------------------


def test_a_real_test_has_no_findings():
    src = ("import pytest\n\ndef test_adds():\n    assert 1 + 1 == 2\n\n"
           "def test_raises():\n    with pytest.raises(ValueError):\n        int('x')\n")
    assert tp.python_findings("tests/test_x.py", src) == []


@pytest.mark.parametrize("source, rule", [
    ("import pytest\n@pytest.mark.skip\ndef test_a():\n    assert f() == 1\n", "skip"),
    ("import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test_a():\n    assert f()\n", "skip"),
    ("import pytest\n@pytest.mark.xfail\ndef test_a():\n    assert f()\n", "skip"),
    ("import unittest\n@unittest.skip('x')\ndef test_a():\n    assert f()\n", "skip"),
    ("import pytest\ndef test_a():\n    pytest.skip('later')\n    assert f()\n", "skip"),
    ("import pytest\npytestmark = pytest.mark.skip\ndef test_a():\n    assert f()\n", "skip"),
    ("def test_a():\n    assert True\n", "trivial-assert"),
    ("def test_a():\n    assert 'ok'\n", "trivial-assert"),
    ("def test_a():\n    x = f()\n    assert x == x\n", "trivial-assert"),
    ("def test_a():\n    pass\n", "empty"),
    ("def test_a():\n    '''later'''\n", "empty"),
    ("def test_a():\n    f()\n", "no-assert"),
    ("def test_a():\n    f()\n    return 'passed'\n", "fake-pass"),
    ("def test_a():\n    try:\n        assert f() == 2\n    except AssertionError:\n        pass\n", "swallowed"),
    ("def test_a():\n    try:\n        assert f() == 2\n    except Exception:\n        print('x')\n", "swallowed"),
    ("import sys\ndef test_a():\n    assert f()\n    sys.exit(0)\n", "exit"),
    ("def pytest_runtest_makereport(item, call):\n    return None\n", "hook"),
    ("def pytest_collection_modifyitems(items):\n    items.clear()\n", "hook"),
    ("def test_a(monkeypatch):\n    monkeypatch.setattr('_pytest.runner.x', 1)\n    assert f()\n", "hook"),
    ("def test_a(:\n", "syntax"),
])
def test_each_bypass_is_found(source, rule):
    assert rule in rules(source)


def test_a_swallowed_failure_that_reraises_is_allowed():
    src = ("def test_a():\n    try:\n        assert f() == 2\n    except AssertionError:\n"
           "        log()\n        raise\n")
    assert "swallowed" not in rules(src)


def test_outcome_hooks_are_found_outside_tests_too():
    findings = tp.file_findings("plugins/report.py", "def pytest_report_teststatus(report):\n    pass\n")
    assert [f.rule for f in findings] == ["hook"]


def test_counting_checks_per_test():
    src = ("import pytest\n\ndef test_a():\n    assert f() == 1\n    assert g() == 2\n\n"
           "class TestB:\n    def test_c(self):\n        with pytest.raises(KeyError):\n            h()\n")
    assert tp.test_names("tests/test_x.py", src) == {
        "tests/test_x.py::test_a": 2, "tests/test_x.py::TestB::test_c": 1}


# ---------------------------------------------------------------------------
# JavaScript, workflows and config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source, rule", [
    ("it.skip('x', () => { expect(f()).toBe(1) })", "skip"),
    ("test.todo('x')", "skip"),
    ("xit('x', () => {})", "skip"),
    ("test('x', { skip: true }, () => {})", "skip"),
    ("describe.only('x', () => {})", "only"),
    ("expect(true).toBe(true)", "trivial-assert"),
    ("assert.ok(true)", "trivial-assert"),
    ("process.exit(0)", "exit"),
    ("try { run() } catch (e) {}", "swallowed"),
])
def test_each_js_bypass_is_found(source, rule):
    assert rule in {f.rule for f in tp.js_findings("tests/a.test.ts", source)}


def test_a_commented_out_js_skip_is_not_a_finding():
    assert tp.js_findings("tests/a.test.ts", "// it.skip('old')\nit('x', () => expect(f()).toBe(2))") == []


@pytest.mark.parametrize("line", [
    "run: pytest || true", "continue-on-error: true", "run: pytest --deselect tests/test_a.py::x",
    "run: pytest -k 'not slow'", "run: pytest --ignore tests/slow", "run: ruff check --exit-zero",
    "addopts = -p no:randomly", "xfail_strict = false", "run: pytest -m 'not slow'",
])
def test_each_weakened_test_run_is_found(line):
    assert tp.run_config_findings(".github/workflows/ci.yml", line + "\n")


def test_a_plain_test_run_is_not_a_finding():
    assert tp.run_config_findings(".github/workflows/ci.yml",
                                  "run: python -m pytest --junitxml=junit.xml\n# || true\n") == []


def test_scan_reads_tests_workflows_and_config_and_skips_allowed_files(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("run: pytest || true\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    found = {(f.path, f.rule) for f in tp.scan(tmp_path)}
    assert found == {("tests/test_a.py", "trivial-assert"), (".github/workflows/ci.yml", "ignored-failure")}
    assert {f.path for f in tp.scan(tmp_path, {"tests/test_a.py"})} == {".github/workflows/ci.yml"}


# ---------------------------------------------------------------------------
# Results and durations
# ---------------------------------------------------------------------------

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5">
<testcase classname="tests.test_a" name="test_fast" time="0.004"/>
<testcase classname="tests.test_a" name="test_ok" time="0.120"/>
<testcase classname="tests.test_a" name="test_slow" time="0.350"/>
<testcase classname="tests.test_a" name="test_bad" time="0.01"><failure message="assert 1 == 2"/></testcase>
<testcase classname="tests.test_a" name="test_later" time="0"><skipped message="not yet"/></testcase>
</testsuite></testsuites>"""


def test_junit_results_are_read():
    r = tp.parse_junit(JUNIT)
    assert [c.outcome for c in r.cases] == ["passed", "passed", "passed", "failed", "skipped"]
    assert r.collection_errors == 1
    assert not r.all_passed


def test_a_test_over_200_ms_fails_and_over_50_ms_warns():
    errors, warnings = tp.duration_findings(tp.parse_junit(JUNIT))
    assert len(errors) == 1 and "test_slow took 350 ms" in errors[0]
    assert len(warnings) == 1 and "test_ok took 120 ms" in warnings[0]


def test_skipped_failed_and_unloaded_tests_are_all_errors():
    errors = tp.result_errors(tp.parse_junit(JUNIT))
    assert any("test_bad failed" in e for e in errors)
    assert any("test_later was skipped" in e for e in errors)
    assert any("failed to load" in e for e in errors)


def test_no_tests_at_all_is_an_error():
    assert tp.result_errors(tp.Results()) == ["no tests ran"]


# ---------------------------------------------------------------------------
# Tests first
# ---------------------------------------------------------------------------

GREEN = tp.Results([tp.Case("t::a", 0.01, "passed")])
RED = tp.Results([tp.Case("t::new", 0.01, "failed", "ImportError")])
PASSING = tp.Results([tp.Case("t::new", 0.01, "passed")])


def test_new_tests_that_fail_on_base_and_pass_here_are_accepted():
    assert tp.tdd_errors(["tests/test_a.py", "src/app.py"], "feature", RED, GREEN, [], False) == []


def test_code_without_tests_is_refused():
    errors = tp.tdd_errors(["src/app.py"], "feature", None, GREEN, [], False)
    assert any("write the failing test first" in e for e in errors)


def test_new_tests_that_already_pass_on_base_are_refused():
    errors = tp.tdd_errors(["tests/test_a.py", "src/app.py"], "bug", PASSING, GREEN, [], False)
    assert any("already pass without this change" in e for e in errors)


def test_tests_only_changes_just_have_to_pass():
    assert tp.tdd_errors(["tests/test_a.py"], "chore", PASSING, GREEN, [], False) == []


def test_a_refactor_keeps_the_tests_unchanged():
    assert tp.tdd_errors(["src/app.py"], "refactor", None, GREEN, [], False) == []
    errors = tp.tdd_errors(["src/app.py", "tests/test_a.py"], "refactor", None, GREEN, [], False)
    assert any("must not change tests" in e for e in errors)


def test_removed_or_weakened_tests_need_a_maintainer():
    weakened = tp.weakened_tests({"a::x": 2, "a::y": 1}, {"a::x": 1})
    assert weakened == ["a::y was removed", "a::x now has 1 check(s), down from 2"]
    assert len(tp.tdd_errors(["tests/a.py"], "chore", PASSING, GREEN, weakened, False)) == 2
    assert tp.tdd_errors(["tests/a.py"], "chore", PASSING, GREEN, weakened, True) == []


def test_a_red_suite_never_merges_even_for_a_maintainer():
    red_suite = tp.Results([tp.Case("t::a", 0.01, "failed", "boom")])
    assert tp.tdd_errors(["tests/test_a.py", "src/a.py"], "feature", RED, red_suite, [], True)


def test_docs_ci_and_tools_are_not_code_changes():
    tests, code = tp.code_changes(["README.md", ".github/workflows/ci.yml", "tools/x.py",
                                   "tests/test_a.py", "src/a.py", "tests/conftest.py"])
    assert tests == ["tests/test_a.py"] and code == ["src/a.py"]
