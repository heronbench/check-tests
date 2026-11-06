# check-tests

**Keep a test suite honest.** `check-tests` makes three things true before a pull
request can merge:

1. **Tests come first.** A change's new tests must fail on the base branch's code and
   pass on the branch, so they really test what the change adds.
2. **Tests are never bypassed.** No skipped, faked, swallowed or hidden tests, and no CI
   tricks that let failures through.
3. **Tests are fast.** Every test runs in under 200 ms, and the target is under 50 ms.

It is one Python file using only the standard library, made for teams where AI agents
write code. An agent pushed to "make the build green" is tempted to skip the failing
test, `assert True`, or catch the error and carry on. These checks catch that, and
make the agent fix the code instead.

## Install

```bash
pip install git+https://github.com/heronbench/check-tests
```

Or copy `check_tests.py` into your repository (for example as `tools/check_tests.py`)
and run it with `python tools/check_tests.py`. Python 3.10 or newer; nothing else.

## The three checks

```bash
check-tests scan .
python -m pytest --junitxml=junit.xml && check-tests durations junit.xml
check-tests test-first --base origin/main
```

| Check | Fails when |
|---|---|
| `check-tests scan .` | A test is skipped or expected to fail (`@pytest.mark.skip`, `skipif`, `xfail`, `pytest.skip()`, `it.skip`, `xit`, `.only`); asserts a constant (`assert True`, `expect(true).toBe(true)`); is empty or asserts nothing; returns `"passed"`; catches its own failure without re-raising; exits the process; or the suite is weakened by pytest hooks, `\|\| true`, `continue-on-error`, `--deselect`, `-k "not ..."`, `--ignore`, `-p no:...` or `xfail_strict = false` |
| `check-tests durations junit.xml` | Any test takes over 200 ms (`--limit-ms`); over 50 ms is a warning (`--target-ms`); or no tests ran |
| `check-tests test-first --base origin/main` | Code changed with no test change; the new or changed tests already pass on the base branch's code; a test was removed or lost assertions and the author is not a maintainer (`--maintainer`); or the suite is not fully green on the branch |

A tests-only change (more tests for existing behaviour) only has to pass. A refactor
(`--card-type refactor`) must change no tests: the existing tests prove the behaviour
is kept.

Every check reports; none changes a result. A failing test is fixed at its root cause,
in the code under test.

## In GitHub Actions

Run each check as its own job, then make all three **required status checks** on your
default branch (Settings > Branches, or a ruleset). Include administrators, so nobody
can merge around them.

```yaml
jobs:
  test-policy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install git+https://github.com/heronbench/check-tests
      - run: check-tests scan .

  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements-dev.txt git+https://github.com/heronbench/check-tests
      - run: python -m pytest --junitxml=junit.xml
      - run: check-tests durations junit.xml

  test-first:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements-dev.txt git+https://github.com/heronbench/check-tests
      - run: check-tests test-first --base "origin/${{ github.base_ref }}"
```

Add a `CODEOWNERS` entry for your workflows and test configuration, so a person
reviews any change to how tests run. This repository's own CI is a working example.

## Limits

- `test-first` runs Python tests with pytest. `scan` also reads JavaScript and
  TypeScript tests, workflows, `package.json` and test configuration.
- A wrong test that passes cannot be caught by code. Review still matters.
- A maintainer can remove or weaken a test; that is deliberate.

## Licence

MIT. Made by [Heron Bench](https://heronbench.com).
