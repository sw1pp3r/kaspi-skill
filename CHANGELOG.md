# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-07-23

### Fixed

- Make CLI import and `--help` work in read-only agent sandboxes by resolving the QR cache only when QR output is requested.
- Document and test one-command installation, discovery, and explicit invocation in both Claude Code and Codex.

## [0.1.0] - 2026-07-23

### Added

- Agent Skill contract for decision-ready Kaspi shopping research.
- Dependency-free Python CLI with `search`, `details`, `shortlist`, and `location` commands.
- Hard delivery gates for today, tomorrow, fast, and any verified date.
- Relevance filtering, model-aware deduplication, and specification conflict detection.
- Seller-offer price and absolute delivery-date verification.
- Official Kaspi app QR capture with a privacy-preserving fallback contract.
- Fixture-backed tests, cross-platform CI, security and contribution guidance.

[Unreleased]: https://github.com/sw1pp3r/kaspi-skill/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/sw1pp3r/kaspi-skill/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/sw1pp3r/kaspi-skill/releases/tag/v0.1.0
