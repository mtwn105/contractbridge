# Contributing

Use Python 3.14 and Node.js 24. Start with the [README](README.md) and the
[low-level design](docs/low-level-design.md). Record architectural changes in
`docs/decisions/`.

## Engineering rules

- Keep reference fixtures, model proposals, trusted templates, and generated
  artifacts distinct. Reference output is never evidence of live inference.
- Models propose validated mapping data. Never execute model-generated code.
- Require recorded semantic review, passing tests, and approval of the exact
  artifact before activation. Candidate verification alone grants no approval.
- Keep credentials, private payloads, and production traffic out of fixtures,
  exports, logs, telemetry, and pull requests.
- Run affected checks, report their actual outcomes, and retain failed results.

## Checks

```sh
uv sync --python 3.14
python3 scripts/install_oasdiff.py
PATH="$PWD/.local/bin:$PATH" .venv/bin/python -m pytest -q
.venv/bin/ruff check src tests ops scripts examples
npm --prefix apps/web ci --ignore-scripts
npm --prefix apps/web test
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
```

Full inference and isolated container verification also require the running Docker
stack. See [verification](docs/verification.md) for the integration check and its
synthetic-data boundary.

## What belongs in Git

Commit source, tests, synthetic fixtures, trusted templates, dependency lockfiles,
Docker configuration, API schemas, and reusable documentation. Keep `.env.example`
free of credentials and account-specific values.

Generated reports, screenshots, recordings, databases, adapter exports, local
credentials, IDE settings, agent state, planning notes, and submission drafts are
ignored. Preserve useful local evidence under ignored paths; do not commit it.
Do not force-add excluded files. Review `git status --short` and the staged diff
before committing. Ignore rules do not remove a file from existing Git history.
