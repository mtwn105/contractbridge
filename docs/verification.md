# Verification and evidence

Reference fixtures, model proposals, generated artifacts, and trusted templates
are different evidence classes. Do not substitute one for another.

## Reproducible checks

The Python suite covers contract/plan validation, scoped gateway authorization,
fixture verification, stale-artifact rejection, isolated generation, publication
constraints, signed webhook handling, and dual-repair retirement gates. Publisher
tests using an HTTP transport double do not prove a remote GitHub PR exists.

The frontend suite covers the operator console and its server-side boundary. The
production build verifies TypeScript and bundling, not browser behavior.

```sh
uv sync --python 3.14
python3 scripts/install_oasdiff.py
PATH="$PWD/.local/bin:$PATH" .venv/bin/python -m pytest -q
.venv/bin/ruff check src tests ops scripts examples
npm --prefix apps/web ci --ignore-scripts
npm --prefix apps/web test
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.example.yml config --quiet
```

Preserve failed outcomes as failed. Generated reports belong under ignored local
paths, not versioned proof files.

The optional browser smoke check exercises the live operator sign-in and console
in Chromium. Playwright is part of the development dependency group; install the
browser binary once:

```sh
.venv/bin/python -m playwright install chromium
set -a; . ./.env; set +a
.venv/bin/python scripts/browser_smoke.py
```

## Live evidence gates

A complete controlled demonstration should independently establish:

1. the unchanged application succeeds against provider v1;
2. the provider changes to v2 and the same application request fails;
3. the gateway sends the failed POST once and records an incident without payloads;
4. actual Ollama/Gemma returns the recorded mapping and provenance;
5. an actual isolated Docker verification report passes its declared fixtures;
6. the exact approved temporary adapter restores a new request;
7. a real GitHub URL is shown only after remote publication succeeds;
8. merge alone leaves the bridge active;
9. matching deployment and native synthetic probes retire it;
10. a negative or ambiguous change stops rather than inventing a repair.

Run the optional example with `./scripts/bootstrap.sh --example shopping`; the
provider version file is ignored and can be switched locally. Restore it after the
exercise. Never use production credentials or traffic as demo fixtures.

## Evidence labels

- A hand-authored reference plan proves interpreter behavior, not AI quality.
- A mock or fixture proves deterministic logic, not a live provider interaction.
- A unit-test transport proves request construction, not a remote service result.
- A Dockerfile or Compose file proves configuration intent, not that isolation ran.
- A candidate test pass is not semantic review or activation approval.
- A merged PR is not a deployed application.
- A successful deployment event is not native-path health.
- A manually signed local webhook is not hosted Sentry/GitHub delivery.

Record model identity, prompt schema version, latency, tests, artifact hashes,
publication URL, commit SHA, deployment environment, verification samples, and
failures from the same run. Redact secrets and bodies before sharing evidence.
