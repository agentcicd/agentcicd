# Release Notes

The Python package is configured in `pyproject.toml`.

Current package facts:

- package name: `agentcicd`
- Python requirement: `>=3.10,<3.14`
- console script: `agentcicd = "agentcicd.cli:main"`
- package data includes SQL builtin fixture manifest, inspection schema, and built UI static assets

Optional dependency groups include:

- `spark`
- `fixtures`
- `sandbox`
- `test`
- `dev`

The repository contains `.github/workflows/release.yml`; review that workflow before publishing a release.

Before inviting external contributors or distributing the repository as open source, add an OSI-approved license.
