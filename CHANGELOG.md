# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [0.2.0] - 2026-10-03

### Added
- Replay of end-to-end response headers and cookies for duplicate requests.
- Query strings in request fingerprints to distinguish requests with different query parameters.
- Native `prek.toml` hook configuration and the prek GitHub Actions integration.

### Changed
- Expired idempotency keys can be reused for a new request, with their expiration window restarted.
- Exempt paths bypass required idempotency-header enforcement.
- Streaming responses are not cached; their idempotency records are removed after the response is produced.

### Documentation
- Clarified request-size bypass behavior and response data retention for bodies, headers, and cookies.

## [0.1.0] - 2026-08-26

### Added
- Initial release of the package.
- Idempotent retry support for POST/PUT/PATCH requests.
- Django model-based key storage with expired-record cleanup support.
- Optional Celery cleanup task and initial documentation.
