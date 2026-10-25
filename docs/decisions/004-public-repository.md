# Public repository boundary

The public repository contains the implementation, tests, independent synthetic
fixtures, trusted generation templates, lockfiles, Docker configuration, and
reusable documentation. Long original proposals, local agent instructions,
submission drafts, personal tooling configuration, and generated run evidence
remain on the development machine and are excluded by `.gitignore`.

The public engineering rules live in `CONTRIBUTING.md`. Current behavior is
documented in the README, architecture, runtime guide, API schema, and decision
records. Public documents do not link to excluded implementation-session files.
Failed checks remain preserved locally; exclusion is not a passing verification.

Docker build contexts exclude the same private/generated material. Verification
scripts recreate ignored output directories in a fresh checkout. Source-map
upload targets use environment configuration instead of a personal Sentry
organization hardcoded in source.
