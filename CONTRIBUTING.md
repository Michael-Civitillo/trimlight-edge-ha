# Contributing to Trimlight Edge

Thanks for helping improve this integration! Here's how to get set up.

## Development Environment

**Requirements:** Python 3.12+

```bash
git clone https://github.com/Michael-Civitillo/trimlight-edge-ha
cd trimlight-edge-ha
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements_test.txt
```

## Running Tests

```bash
pytest tests/ -v
```

With coverage:
```bash
pytest tests/ --cov=custom_components/trimlight --cov-report=term-missing
```

## Making Changes

1. Fork the repo and create a branch: `git checkout -b feat/my-feature`
2. Make your changes
3. Run tests and ensure they pass
4. Bump the `version` in `custom_components/trimlight/manifest.json` if it's a user-facing change
5. Add an entry to `CHANGELOG.md`
6. Open a pull request — fill out the PR template

## Reporting Bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml). Include:
- Your HA version
- Integration version
- Relevant logs (enable debug logging: add `trimlight: debug` under `logger > logs` in `configuration.yaml`)

## Requesting Features

Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.yml).

## Code Style

- Follow existing patterns in the codebase
- Keep functions small and focused
- Type-hint all function signatures
