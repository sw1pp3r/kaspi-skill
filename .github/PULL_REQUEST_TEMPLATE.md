## What this changes

<!-- One or two sentences. Link the issue with "Fixes #N" when applicable. -->

## Why

<!-- Describe the shopping failure or evidence gap this addresses. -->

## Test plan

- [ ] `python3 -m py_compile scripts/kaspi.py`
- [ ] `python3 -m unittest discover -s tests -v`
- [ ] `python3 scripts/kaspi.py --help`
- [ ] New network behavior is fixture-backed; tests do not require live Kaspi traffic
- [ ] No exact address, cookie, token, account, cart, or order data is persisted
- [ ] Request budgets and strict delivery gates remain intact
- [ ] `CHANGELOG.md` updated under `Unreleased`
