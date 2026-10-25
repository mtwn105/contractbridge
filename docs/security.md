# Security boundaries

ContractBridge automates a narrow repair workflow; it is not a general-purpose
code-execution agent or transparent network interceptor.

## Trust model

The operator, static integration configuration, trusted templates, worker, and
deployment allowlists are trusted. Provider specifications, fixtures, model
output, runtime payloads, and webhooks are untrusted.

The local stack is single-operator. A public multi-tenant service would need
tenant isolation, RBAC, rate limiting, a managed secret store, production Temporal
and database operations, and a deployment-specific threat review.

## Model and contract boundary

- Specifications are bounded and parsed without remote references or unsafe YAML
  features. Unsupported schemas fail closed.
- Model prompts treat imported descriptions as data. The model has no shell,
  network, GitHub, Docker, or deployment tool.
- Model output must match the versioned plan schema. Paths, types, enum coverage,
  required fields, and units are validated independently.
- The model supplies no Python. Trusted templates render deterministic adapters
  from validated JSON resources.
- Fixture data must be explicitly synthetic or sanitized. ContractBridge cannot
  prove that arbitrary user data has been anonymized.

## Gateway boundary

Every integration has an HMAC-derived scoped bearer token. It authorizes only the
matching connection's gateway/native operation; it is not an operator credential.
The browser never receives the operator token or example service secrets.

The gateway accepts only supported JSON POSTs, makes one upstream request, disables
redirects, and never retries an ambiguous write. Provider credentials and
authentication migrations are not supported. Upstreams must match exact server
and worker allowlists.

Runtime request/response bodies are validated in memory and not stored in incident,
audit, Temporal, GitHub, or Sentry records.

## Review, build, and activation

Contract, mapping, fixture, template, dependency, behavior, and artifact hashes
bind each decision to exact inputs. A tested candidate is not automatically an
approved runtime. Activation requires recorded semantic review, passing tests,
and approval of the exact artifact checksum. Any relevant input drift invalidates
the decision.

Automatic mitigation is allowed only under an integration-specific policy that
matches the exact old/current contract, fixtures, normalized mapping behavior,
and recorded semantic review. Unexpected transformations stop instead of widening
that policy.

The control plane has no Docker socket. A separate trusted worker does and should
be treated as host-sensitive. Generated verification and runtime containers use
unprivileged users, read-only roots, dropped capabilities, bounded resources,
temporary writable storage, and restricted networks. A Docker configuration file
is not evidence that these controls ran; retain the actual verification report.

## Repository boundary

GitHub credentials remain server-side. The publisher is limited to the registered
repository/base and declared client module/bundle plus generated evidence paths.
It verifies the target's expected baseline before updating it, uses additive Git
trees, and opens a draft PR. It does not merge, deploy, force-push, or execute
repository code.

Signed GitHub webhooks are matched to the recorded publication, branch, pull
request, commit, and per-integration application environment. A merge is not
proof of deployment.

## Deployment verification

Temporary repair retirement permits only registered application origins and
credential environment-variable names. Probes must be explicitly enabled and
synthetic. The release and probe endpoints must prove the deployed commit, exact
repair hash, native-client mode, and translation bypass for multiple samples.
ContractBridge never mirrors arbitrary production POST traffic for verification.

If verification fails or evidence is incomplete, the temporary bridge remains
active and the failure remains visible.

## Webhooks and observability

Sentry and GitHub webhooks use distinct HMAC secrets. Sentry alerts also require a
fresh timestamp and matching connection/project tags. Only bounded identifiers,
event times, and hashes are retained; raw webhook bodies are not.

Sentry SDK configuration strips bodies, headers, cookies, query parameters, user
identity, stack variables, breadcrumbs, prompts, model messages, and fixtures.
Agent spans may include allowlisted model name, token counts, latency, phase, and
failure category. Session replay and prompt capture are disabled.

## Operational limits

The Compose Temporal service is a development server, not a production cluster.
The included app has no automated rollback of an upstream provider, no proof of
business correctness beyond the declared contracts/fixtures, and no safe repair
for authentication changes, removed required data, or unknown semantics.
