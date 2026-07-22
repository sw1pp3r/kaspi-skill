# Security policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Please use a [private GitHub Security Advisory](https://github.com/sw1pp3r/kaspi-skill/security/advisories/new). Do not open a public issue with exploit details, live cookies, exact addresses, account data, or order information.

Include the exact command or input, operating system, Python version, affected commit, expected boundary, and observed behavior. Expect an initial response within five business days.

## Scope and data flow

Kaspi Skill is a networked research utility. It sends bounded public product queries, reads public product pages and seller-offer responses, and can open a public product page in `agent-browser` to capture Kaspi's QR modal. The saved location profile contains city and zone metadata only. The tool does not authenticate, place orders, or store an exact address.

## Threat model

Security-sensitive areas include:

- Rejecting non-Kaspi and lookalike URLs before product-detail requests.
- Treating marketplace HTML and JSON as untrusted input.
- Keeping subprocess arguments structured when invoking `agent-browser`.
- Removing tracking and session parameters from shareable URLs.
- Preventing exact addresses, cookies, and tokens from entering saved profiles or QR targets.
- Bounding query count, result count, detail pages, and request timeouts.
- Making fallback QR generation explicit because it contacts a third-party renderer.

If the tool reaches an undocumented host, exposes private browser state, stores address-level data, bypasses request bounds, or claims an order was placed, treat that as a security or privacy bug.
