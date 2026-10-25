# Generic core and dual repair lifecycle

The accepted product direction changed from an upload-first workbench and then a
shopping-first demonstration to a plug-in API compatibility control plane. The
shopping provider/application now live only in `examples/` and an optional Compose
overlay. The base deployment contains no provider URL, connection ID, fixture,
field mapping, or application token.

This decision supersedes the primary-experience and stop-at-draft-PR portions of
the earlier runtime-repair and shopping-first decisions. Their useful history
remains archived locally and is not part of the published record.

## Shared mapping, two outcomes

One immutable, tested mapping drives a temporary runtime adapter and a permanent
declarative client patch. This prevents two independent agents from guessing
different semantics. The model produces only mapping data; trusted code produces
and verifies both artifacts.

Temporary activation remains conservative: an operator must approve the exact
verified candidate, tests must pass, and activation names the exact built artifact.
There is no integration-specific mitigation allowlist. A rejection records the
operator's comment and feeds it back into the next model proposal.

Permanent repair may publish a real draft PR only to a registered repository/base
and declared client files. It never merges or deploys.

## Durable cleanup

Temporal replaces process-local waiting for the new dual lifecycle. Signed GitHub
events record merge and successful deployment for the exact repair commit and the
integration's application environment. Authorized native synthetic probes then
verify the deployed commit and repair hash with translation bypassed. Only that
evidence retires the exact temporary adapter.

The bundled Temporal SQLite development server is suitable only for a local demo;
a hosted installation needs production Temporal and database operations.

## Interface simplification

The primary UI is an integration and incident console: state, agent progress,
mapping, tests, operator review, mitigation, and PR/deployment status. The console
records an approval or a revision comment for each verified candidate; the
optional shop is demonstration input, not product identity.
