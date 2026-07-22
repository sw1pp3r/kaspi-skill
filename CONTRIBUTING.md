# Contributing

Bug reports, focused feature requests, and pull requests are welcome.

## Before opening an issue

- Check whether the problem is caused by an upstream Kaspi page or endpoint change.
- Remove exact addresses, cookies, account details, and order data from logs.
- Search existing issues for the error text or affected command.

## Bug reports

Use the bug-report template and include:

- Operating system and Python version.
- The project commit or release.
- The exact command with personal data removed.
- Full traceback or error output.
- Whether the failure affects `search`, `details`, `shortlist`, or QR capture.

## Feature requests

Open an issue before writing code. The project keeps bounded traffic, explicit delivery gates, no exact-address storage, and no third-party Python runtime dependencies. Proposals that weaken those constraints need a strong safety and maintenance case.

## Pull requests

1. Create a focused branch.
2. Run `python3 -m py_compile scripts/kaspi.py`.
3. Run `python3 -m unittest discover -s tests -v`.
4. Run `python3 scripts/kaspi.py --help`.
5. Add fixture-backed tests for behavior changes; tests must not depend on live Kaspi traffic.
6. Update `CHANGELOG.md` under `Unreleased`.
7. Explain any new network boundary or stored field in the pull-request body.

## Good first issues

Look for the [`good first issue`](https://github.com/sw1pp3r/kaspi-skill/labels/good%20first%20issue) label. Each starter task includes a code pointer, acceptance criteria, and explicit out-of-scope notes.

## Maintainer

Eugene Bisovka (`sw1pp3r`).
