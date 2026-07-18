# Releasing crossagent

Releases are built and published from GitHub Actions with PyPI Trusted
Publishing. No long-lived PyPI token is stored in GitHub.

## One-time setup

1. Create a `pypi` environment in the GitHub repository and require a trusted
   maintainer's approval before deployment.
2. On PyPI's **Publishing** account page, add a pending GitHub publisher with:

   - PyPI project name: `crossagent`
   - Owner: `datj9`
   - Repository: `crossagent`
   - Workflow: `release.yml`
   - Environment: `pypi`

The pending publisher creates the PyPI project automatically on the first
successful release.

## Publish a version

1. Update `__version__` in `src/crossagent/__init__.py`, then update
   `CHANGELOG.md`. Hatchling reads the package version from `__version__`.
2. Build and validate locally:

   ```bash
   python -m pip install build twine
   python -m build
   python -m twine check --strict dist/*
   pytest -q
   ```

3. Merge the release commit to `main`, then tag that exact commit and push the
   tag:

   ```bash
   git tag -s v0.1.0 -m "crossagent 0.1.0"
   git push origin v0.1.0
   ```

The tag must exactly match the package version. The `release` workflow builds
the wheel and source distribution once, waits for approval in the `pypi`
environment, and publishes those artifacts to PyPI. PyPI release files cannot
be replaced, so fix a failed release with a new version rather than reusing a
published version number.
