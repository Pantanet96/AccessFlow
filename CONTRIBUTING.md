# Contributing

Thanks for considering a contribution to AccessFlow.

## Setup

```bash
python -m venv .venv
. .venv/Scripts/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export DATABASE_PATH=./data/app.db
uvicorn app.main:app --reload
pytest
```

See the [README](README.md#local-development) for the full local development guide.

## Making a change

1. Open an issue first for anything non-trivial, so the approach can be agreed before you invest time.
2. Work on a branch, one topic per branch/PR.
3. Add or update tests for the behavior you change.
4. Make sure `pytest` passes before opening a PR.
5. Keep commit messages and PR descriptions focused on *why*, not just *what*.

## Reporting bugs

Use the issue templates. Include repro steps, expected vs. actual behavior, and relevant logs/config (redact secrets).

## Security issues

Do not open a public issue for security vulnerabilities — see [SECURITY.md](docs/SECURITY.md).
