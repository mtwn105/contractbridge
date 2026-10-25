# ContractBridge — Low-Level Design

This document describes the implementation as it exists in the repository: module
boundaries, persisted state, request/response flows, and the gates that decide
whether a repair may touch traffic. Diagrams name real files and functions so
they can be checked against the code.

Scope note: the shopping provider and shop are an **example instance** of the
generic integration model. Nothing in the core assumes those names, fields, or
that a storefront exists.

---

## 1. Component map

```mermaid
flowchart LR
    subgraph Client["Registered application"]
        APP["App code + client contract"]
    end

    subgraph Control["Control plane (no live traffic)"]
        WEB["web<br/>Next.js UI + server proxies"]
        API["control-plane<br/>Starlette API"]
        PLAN["Planner<br/>OllamaGemma"]
        WF["orchestrator<br/>Temporal worker"]
        PUB["GitHubPublisher"]
    end

    subgraph Worker["Trusted host component"]
        SUP["worker<br/>Docker supervisor"]
        BUILD["sandbox container<br/>network none"]
        PROXY["cb-proxy-buildId<br/>deterministic adapter"]
    end

    subgraph Data["State and evidence"]
        DB[("PostgreSQL<br/>resources + audit")]
        ART[("artifacts volume<br/>frozen source, tests, zips")]
        TMP[("Temporal<br/>durable workflow")]
    end

    subgraph External["External services"]
        OLL["Ollama<br/>gemma3:4b"]
        UP["Upstream provider API"]
        SEN["Sentry"]
        GH["GitHub"]
    end

    APP -->|"scoped gateway token"| API
    WEB -->|"operator token"| API
    API --> DB
    API --> ART
    API -->|"/api/chat JSON"| OLL
    API -->|"file protocol"| SUP
    SUP -->|"docker create/start"| BUILD
    SUP -->|"docker run"| PROXY
    API -->|"current OpenAPI"| UP
    API -.->|"spans + errors"| SEN
    WF -->|"phase activity"| API
    WF --> TMP
    PUB -->|"draft PR only"| GH
    SEN -.->|"signed alert"| API
    GH -.->|"signed merge/deploy"| API
    PROXY -->|"translated calls"| UP
```

Two planes, deliberately separate:

| Plane | Path | Contains AI? |
| --- | --- | --- |
| Data plane | app → gateway → provider, or app → gateway → bridge → provider | No |
| Control plane | incident → investigation → candidate → PR / activation | Yes, only here |

The model never executes inside a request path, and generated source is never
executed as model output — it is rendered by trusted templates from validated
mapping data.

---

## 2. Deployment topology

```mermaid
flowchart TB
    subgraph Host["Docker host (loopback-published ports)"]
        subgraph ControlNet["network: control"]
            WEBC["web :3000"]
            CP["control-plane :8080"]
            DBC[("db :5432")]
            OLLC["ollama :11434"]
            TEMPC["temporal :7233"]
            ORCH["orchestrator"]
            WORK["worker"]
        end

        subgraph AdapterNet["network: adapters (internal: true)"]
            PROV["provider-api :8080"]
            SHOP["shopping-api :8080"]
            ING["ingress :8080"]
            BR["cb-proxy-&lt;buildId&gt; :8080"]
        end
    end

    BROWSER["Browser"] -->|"127.0.0.1:3000"| WEBC
    CURL["Operator / CI"] -->|"127.0.0.1:8000"| CP
    LOCAL["Local client"] -->|"127.0.0.1:18080/&lt;buildId&gt;/path"| ING
    WEBC --> CP
    CP --> DBC
    CP --> OLLC
    CP --> TEMPC
    ORCH --> TEMPC
    ORCH --> CP
    WORK -->|"docker socket"| DOCKERD["dockerd"]
    CP -.->|"adapters network"| PROV
    ING --> BR
    CP --> BR
    SHOP -->|"gateway token"| CP
```

Ports and boundaries:

| Service | Published | Network(s) | Notes |
| --- | --- | --- | --- |
| `web` | `127.0.0.1:3000` | control | Holds `OPERATOR_TOKEN` server-side only |
| `control-plane` | `127.0.0.1:8000` | control, adapters | API + gateway; read-only root, all caps dropped |
| `ingress` | `127.0.0.1:18080` | control, adapters | Only ingress is publicly useful; adapters stay internal |
| `worker` | none | control | **Only** component with `/var/run/docker.sock` |
| `provider-api` (example) | `127.0.0.1:18081` | adapters | Switchable v1/v2 for demos |
| `shopping-api` (example) | none | control | Calls the gateway with a connection-scoped token |
| `db`, `ollama`, `temporal` | none | control | Not reachable from the host |

Restart-safe state lives in PostgreSQL and the `contractbridge_artifacts`
volume. Containers, Temporal workflows, and adapters are recreatable.

---

## 3. Source map

| Concept | Module |
| --- | --- |
| HTTP surface, auth guard, error mapping | `src/contractbridge/api.py` |
| Gateway, incidents, repair orchestration, routing | `src/contractbridge/monitoring.py` |
| Operator-facing runtime routes | `src/contractbridge/monitoring_api.py` |
| Dual lifecycle (mitigate / permanent / verify) | `src/contractbridge/dual_repair.py` |
| Durable workflow and phase activity | `src/contractbridge/orchestration.py` |
| Gemma prompt, grammar, provenance | `src/contractbridge/planner.py` |
| Candidate JSON schema from real fields | `src/contractbridge/candidates.py` |
| Trusted code generation and hashes | `src/contractbridge/generation.py` |
| File protocol to the Docker worker | `src/contractbridge/sandbox.py` |
| Container isolation and test parsing | `ops/worker.py` |
| Draft PR creation | `src/contractbridge/publishing.py` |
| Signed Sentry/GitHub webhooks | `src/contractbridge/hooks.py` |
| Revisioned aggregates + audit | `src/contractbridge/resources.py`, `storage.py` |
| Integration registration, tokens, client targets | `src/contractbridge/integrations.py` |
| Automatic-mitigation policy | `src/contractbridge/mitigation_policy.py` |
| Contract parsing / strict schema normalisation | `src/contractbridge/contracts.py` |
| Deterministic transformation interpreter | `src/contractbridge/engine.py` |
| Ingress for UUID-addressed adapters | `src/contractbridge/ingress.py` |
| Browser console and server proxies | `apps/web/app/repair-console.tsx`, `apps/web/app/api/**` |

---

## 4. Persistence model

Two families: **migrations** (the classic spec-to-spec workflow and its build
history) and **runtime resources** (connections, incidents, webhook receipts).
Every write is a revision-guarded document update plus an append-only event.

```mermaid
erDiagram
    MIGRATIONS ||--o{ MIGRATION_REVISIONS : "snapshot per revision"
    MIGRATIONS ||--o{ AUDIT_EVENTS : "append-only"
    MIGRATIONS ||--o{ JOBS : "analyze/propose/build/activate"
    RUNTIME_RESOURCES ||--o{ RUNTIME_EVENTS : "append-only"

    MIGRATIONS {
        string id PK
        bigint revision
        string document "JSON aggregate"
    }
    MIGRATION_REVISIONS {
        string migration_id PK
        bigint revision PK
        string document
    }
    AUDIT_EVENTS {
        string id PK
        string migration_id
        string created_at
        string document
    }
    JOBS {
        string id PK
        string migration_id
        string document "kind,status,buildId"
    }
    RUNTIME_RESOURCES {
        string kind PK "connections or incidents or hooks"
        string id PK
        bigint revision
        string document
    }
    RUNTIME_EVENTS {
        string id PK
        string resource_id
        string document "action,revision,metadata"
    }
```

Rules enforced in `resources.py` / `storage.py`:

- `update(kind, id, action, visitor, expected_revision)` runs under a process lock
  plus `SELECT … FOR UPDATE` (PostgreSQL) and a conditional `UPDATE … WHERE
  revision = ?`; a lost race raises `Conflict` (HTTP 409).
- `If-Match`/`expected` revision prevents two operators or a retried activity from
  acting on stale state.
- `runtime_events` records the action, revision, and a small metadata subset
  (candidate artifact hash, publication authorization) — **never** request or
  response bodies.

Representative documents (abridged):

```jsonc
// connections
{
  "id": "…", "revision": 7,
  "name": "Shopping checkout",
  "upstreamBaseUrl": "http://provider-api:8080", "specPath": "/openapi.json",
  "sourceSpec": { /* parsed OpenAPI */ }, "sourcePath": "/v1/orders",
  "fixtures": [ /* synthetic request/response expectations */ ],
  "autoRepair": true, "dualRepair": true,
  "state": "MONITORING",                  // MONITORING | DEGRADED
  "route": { "mode": "DIRECT" },          // or BRIDGE + migrationId + buildId
  "generation": 0, "openIncidentId": null, "repairIncidentId": null,
  "baselineHash": "…", "autoPublishDraft": false,
  "client": { "kind": "python-declarative", "modulePath": "...", "bundlePath": "..." },
  "application": { "baseUrl": "...", "credentialEnv": "…", "environment": "staging" },
  "mitigationAuthorization": { "oldHash": "…", "newHash": "…", "fixturesHash": "…",
                               "behaviorHash": "…", "semanticReview": "…" }
}

// incidents
{
  "id": "…", "connectionId": "…", "kind": "HTTP_CONTRACT_MISMATCH",
  "state": "READY_FOR_REVIEW", "running": false,
  "occurrences": 1, "migrationId": "…",
  "candidate": { "artifactHash": "…", "inputHash": "…", "tests": 12,
                 "verificationMode": "isolated-docker", "semanticReview": "PENDING" },
  "lifecycle": {
    "phase": "GITHUB_NOT_CONFIGURED",
    "sharedMappingHash": "…",
    "mitigation": { "status": "REVIEW_REQUIRED" },
    "permanent":  { "status": "VERIFIED", "patchHash": "…", "applicationFiles": ["…"] }
  }
}
```

---

## 5. Trust boundaries and invariants

| Caller | Credential | May do |
| --- | --- | --- |
| Operator UI / CLI | `Authorization: Bearer <OPERATOR_TOKEN>` | Everything in `/api/v1/**` except gateway/native data calls |
| Registered application | `Bearer cbg.<connectionUUID>.<hmac>` | Exactly one connection's gateway operation, POST only |
| Sentry | `Sentry-Hook-Signature` HMAC + timestamp | Create/link an incident for a **registered** project and connection |
| GitHub | `X-Hub-Signature-256` HMAC | Record merge/deployment evidence for a **registered** repository |
| Generated adapter | none (network position) | Interpret one frozen mapping artifact; cannot reach the API or Docker socket |

The guard in `api.py` treats only `/health`, `/openapi.json`, the two hook paths,
and valid `cbg.` tokens as unauthenticated-with-scope; everything else needs the
operator token.

Invariants the code enforces:

1. **One upstream attempt per client request.** `_gateway` calls `exchange` once
   and reports `requestRetried: false`; background repair never re-POSTs the
   business operation.
2. **No payload retention.** Incidents store status codes, hashes, and metadata;
   Sentry events carry only connection/incident IDs and hashed contexts.
3. **The model proposes data only.** `OllamaPlanner` sends a JSON grammar built
   from real schema paths; `assemble` + `inspect_plan` validate the result; a
   blocker stops the flow even if a human approves.
4. **Activation names an exact artifact.** `workflow.submit("activate")` requires
   `state == READY_FOR_APPROVAL`, `confirmed is True`, and the SHA-256 of the
   on-disk `adapter.zip` to equal the approved hash.
5. **Automatic mitigation needs a pinned policy.** The policy hashes must match
   the exact old spec, new spec, fixtures, and normalised plan behaviour.
6. **A merged PR is not a deployment.** Retirement needs a successful deployment
   for the same commit and environment, then native bypass probes.
7. **Generated code never runs on the host.** Builds execute in a container with
   `--network none`, read-only root, `10001:10001`, 512 MB, 2 CPUs, pids 64, no
   Docker socket; the supervisor asserts those values from `docker inspect`
   before accepting a report.

---

## 6. Flows

### 6.1 Registration and baseline

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operator
    participant API as control-plane
    participant REG as integrations.py
    participant DB as runtime_resources

    OP->>API: POST /api/v1/connections (or INTEGRATIONS_FILE at boot)
    API->>API: validate upstream origin against UPSTREAM_ALLOWLIST
    API->>REG: parse sourceSpec, select sourcePath
    API->>API: validate fixtures against the client contract, reject secrets
    API->>API: validate client target, application endpoints, mitigation policy
    API->>DB: create connection (baselineHash, route DIRECT, generation 0)
    API-->>OP: connection + gatewayPath + exampleRequest
```

The gateway path is derived, not chosen by the caller:
`/api/v1/gateway/<connectionId><sourcePath>`. The client credential is
`gateway_token(GATEWAY_SIGNING_SECRET, connectionId)`.

### 6.2 Healthy monitored call

```mermaid
sequenceDiagram
    autonumber
    participant APP as Registered application
    participant GW as gateway (monitoring.py)
    participant CT as contracts.py
    participant UP as Provider API

    APP->>GW: POST /api/v1/gateway/{id}/v1/orders + cbg token
    GW->>GW: resolve connection, check method and operation
    GW->>CT: validate_payload(old.request, payload)
    alt invalid client payload
        GW-->>APP: 422, no upstream call
    else valid
        GW->>UP: exactly one POST
        UP-->>GW: status + body
        alt status matches and body validates
            GW->>GW: record_success (state MONITORING, generation++)
            GW-->>APP: same status + client-shaped body
        else status or schema mismatch
            GW->>GW: record_failure → incident
            GW-->>APP: 502 with incidentId, requestRetried false
        end
    end
```

### 6.3 Drift detection and incident de-duplication

```mermaid
sequenceDiagram
    autonumber
    participant GW as gateway
    participant RS as resources
    participant BG as background task

    GW->>RS: get connection
    alt connection already has openIncidentId
        RS-->>GW: existing incident
    else new failure
        GW->>GW: key = sha(connection, generation, kind, status, baselineHash)
        GW->>RS: get incident uuid5(key)
        alt not present
            GW->>RS: create incident (occurrences 0)
        end
    end
    GW->>RS: bump occurrences and lastSeen
    GW->>RS: connection DEGRADED, failureCount++, openIncidentId
    alt first occurrence only
        GW->>GW: capture_failure to Sentry (ids only)
        alt autoRepair and INCIDENT_TRIGGER != sentry
            GW->>BG: dispatch_repair(incidentId)
        end
    end
```

Why the deterministic ID matters: repeated failing traffic cannot spawn repeated
model runs, and a retry of the same failure collapses onto the same incident. The
connection `generation` changes only when a success resolves the incident or a
route is promoted, so the next unrelated drift gets a fresh incident.

### 6.4 Investigation → verified candidate

```mermaid
sequenceDiagram
    autonumber
    participant API as control-plane
    participant UP as Provider API
    participant MIG as migrations/jobs
    participant PLAN as OllamaPlanner
    participant GEN as Generator
    participant SUP as worker (Docker)

    API->>API: claim incident (running=true, FETCHING_SPEC)
    alt kind is auth / rate limit / timeout / unavailable
        API->>API: ACTION_REQUIRED, stop
    end
    API->>UP: GET current OpenAPI at specPath
    UP-->>API: specification
    alt digest equals baselineHash
        API->>API: NO_CONTRACT_CHANGE, stop
    end
    API->>API: resolve target POST path (declared, operationId, or same path)
    alt no supported target
        API->>API: ACTION_REQUIRED, stop
    end
    API->>MIG: create migration, upload old/new specs and fixtures
    API->>MIG: job analyze → pinned oasdiff → DIFF_READY
    API->>PLAN: propose(old, new, fixtures)
    PLAN-->>API: plan JSON + provenance (model, tokens, latency, prompt version)
    API->>API: inspect_plan — blockers vs review-required issues
    alt unsupported or missing information
        API->>API: BLOCKED, stop
    end
    alt no fixtures
        API->>API: NEEDS_FIXTURES, stop
    end
    API->>API: verify_fixtures on the proposal
    alt any fixture fails
        API->>API: BLOCKED, stop
    end
    API->>GEN: render frozen source for candidate id
    GEN-->>API: sourceHash + inputHash
    API->>SUP: build(candidateId, sourceHash)
    SUP-->>API: tests passed + sandbox introspection + artifactHash
    API->>API: re-fetch spec and compare with approved upstream
    API->>MIG: READY_FOR_REVIEW with candidate evidence
```

`assert_current` runs again before approval, publication, and activation: if the
provider changed while the agent was thinking, the repair is refused rather than
silently applied to a different contract.

### 6.5 Sandbox verification (worker side)

```mermaid
sequenceDiagram
    autonumber
    participant API as control-plane
    participant Q as artifacts/queue
    participant W as worker
    participant D as dockerd
    participant C as cb-build-id

    API->>Q: write job file {action: build, buildId, sourceHash}
    W->>Q: poll queue
    W->>W: verify source-manifest checksums
    W->>D: create (network none, read-only, uid 10001, 512m, 2 cpu, pids 64)
    W->>C: start --attach (compile + pytest)
    C-->>W: junit xml + stages.json
    W->>W: docker inspect and assert isolation fields
    W->>W: require tests >= 1 and zero failures/errors/skips
    W->>Q: write result (passed, artifactHash, sandbox, cases)
    API->>Q: read matching result file
    API->>API: require sourceHash match and tests >= fixture count
```

A timeout or missing result fails the repair; the API never treats an unverified
build as a pass.

### 6.6 Runtime mitigation gate

```mermaid
flowchart TD
    A["Candidate verified<br/>READY_FOR_REVIEW"] --> B{"AUTO_MITIGATE enabled<br/>and policy present?"}
    B -->|no| C["REVIEW_REQUIRED<br/>AWAITING_RUNTIME_REVIEW"]
    B -->|yes| D{"policy hashes match<br/>old / new / fixtures / behaviour?"}
    D -->|no| C
    D -->|yes| E["approve with policy semantic review"]
    E --> F["build from approved inputs"]
    F --> G{"artifact hash equals<br/>approved build output?"}
    G -->|no| H["Conflict, no activation"]
    G -->|yes| I["activate build<br/>route = BRIDGE"]
    I --> J["MITIGATED<br/>traffic through adapter"]
```

The policy is intentionally narrow. In the bundled example, a Gemma mapping whose
normalised behaviour differs from the reviewed reference produces
`REVIEW_REQUIRED` — that is the gate working, not a failure. Once a human or a
matching policy authorises it:

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operator or policy
    participant API as control-plane
    participant MIG as workflow
    participant SUP as worker
    participant GW as gateway

    OP->>API: approve current exact inputs
    API->>MIG: submit build (requires approvalHash == fingerprint)
    API->>SUP: build(sourceHash)
    SUP-->>API: passing report + artifact.zip
    OP->>API: activate (confirmed, artifactHash)
    API->>API: compare sha256(adapter.zip) with approved hash
    API->>SUP: activate(buildId, artifactHash, upstream)
    SUP-->>API: cb-proxy-buildId running on adapters
    API->>API: connection route = BRIDGE, temporary=true
    GW->>GW: subsequent calls target cb-proxy-buildId port 8080
```

### 6.7 Permanent application repair (draft PR)

```mermaid
sequenceDiagram
    autonumber
    participant API as dual_repair.py
    participant PKG as package()
    participant PUB as GitHubPublisher
    participant GH as GitHub API

    API->>PKG: render trusted client module + bundle from the same mapping
    API->>API: require bundle.repairId == candidate.inputHash
    API->>API: require declared client target and shared directory
    alt GitHub not configured
        API->>API: GITHUB_NOT_CONFIGURED, stop with patch available
    end
    API->>API: check publicationAuthorization == current policy
    API->>PUB: publish(incident, files, body, expected baseline)
    PUB->>GH: GET base ref and commit
    PUB->>GH: GET target file contents on base
    alt baseline differs
        PUB-->>API: Conflict, refuse to overwrite
    end
    PUB->>GH: POST git/tree, POST git/commit
    PUB->>GH: POST git/refs (contractbridge/repair/...-application-...)
    PUB->>GH: POST pulls (draft)
    GH-->>PUB: PR number and URL
    PUB-->>API: publication receipt (url, branch, headSha, base)
    API->>API: PR_OPEN
```

The PR body carries the mapping table, verification evidence, and the business
assumptions that still need review. ContractBridge never merges and never
deploys.

### 6.8 Sentry-triggered investigation

```mermaid
sequenceDiagram
    autonumber
    participant SEN as Sentry
    participant HOOK as /hooks/sentry
    participant RS as resources
    participant API as control-plane

    SEN->>HOOK: event_alert (triggered) + HMAC + timestamp
    HOOK->>HOOK: verify HMAC and reject older than 600 s
    HOOK->>HOOK: require resource event_alert and action triggered
    HOOK->>RS: resolve connection from tag contractbridge.connection_id
    alt unknown connection or mismatched project
        HOOK-->>SEN: 202 accepted false (no incident)
    end
    HOOK->>RS: link or create incident from tag contractbridge.incident_id
    HOOK->>RS: dedupe receipt (same raw payload → same id)
    alt first receipt
        HOOK->>API: background dispatch_repair(incidentId)
    else duplicate
        HOOK-->>SEN: 202 duplicate true (no second run)
    end
```

Two Sentry roles stay separate: the monitored-application project supplies the
failure signal; the ContractBridge projects trace the agent itself.

### 6.9 Merge, deployment, bypass probes, retirement

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant HK as /hooks/github
    participant WF as Temporal
    participant API as dual_repair.verify
    participant APP as Deployed application
    participant SUP as worker

    GH->>HK: pull_request closed + merged
    HK->>HK: verify HMAC, registered repo, branch/headSha/base
    HK->>HK: store receipt (merge sha)
    GH->>HK: deployment_status success (same sha, environment)
    HK->>HK: store receipt, reconcile out of order
    HK->>WF: signal wake
    WF->>API: phase verify
    alt merge or deployment identity does not match
        API-->>WF: ready false, bridge stays ACTIVE
    end
    loop N samples (2..10)
        API->>APP: GET release (commit, repairHash, mode=native)
        API->>APP: POST probe (translation=bypassed, status=CONFIRMED)
    end
    alt any sample fails
        API->>API: VERIFY_FAILED, bridge stays ACTIVE
    end
    API->>API: re-read release evidence and re-check provider spec
    API->>API: persist RETIRING with successful samples
    API->>SUP: stop(buildId)
    API->>API: route = DIRECT, mitigation RETIRED, phase RESOLVED
```

The `RETIRING` checkpoint exists so a crash between "samples passed" and "adapter
stopped" resumes retirement instead of re-running the synthetic operation.

---

## 7. State machines

### 7.1 Incident repair state

```mermaid
stateDiagram-v2
    [*] --> DETECTED
    DETECTED --> FETCHING_SPEC: dispatch_repair
    FETCHING_SPEC --> ANALYZING: spec fetched, target resolved
    FETCHING_SPEC --> ACTION_REQUIRED: auth, rate limit, timeout, unavailable
    FETCHING_SPEC --> NO_CONTRACT_CHANGE: digest equals baseline
    ANALYZING --> PROPOSING: oasdiff job passed
    PROPOSING --> BLOCKED: unsupported or missing information
    PROPOSING --> NEEDS_FIXTURES: no verification fixtures
    PROPOSING --> BLOCKED: fixture verification failed
    PROPOSING --> VERIFYING_CANDIDATE: proposal matches fixtures
    VERIFYING_CANDIDATE --> READY_FOR_REVIEW: sandbox tests passed
    VERIFYING_CANDIDATE --> FAILED: sandbox timeout or mismatch
    READY_FOR_REVIEW --> RESOLVED: bridge retired or operator route
    READY_FOR_REVIEW --> FAILED: publication or activation error
    DETECTED --> RESOLVED: later success observed, nothing activated
    FAILED --> FETCHING_SPEC: retry
```

### 7.2 Dual lifecycle phase

```mermaid
stateDiagram-v2
    [*] --> DETECTED
    DETECTED --> INVESTIGATING
    INVESTIGATING --> UNSUPPORTED: blocked or no repair
    INVESTIGATING --> CANDIDATE_VERIFIED: candidate passed
    CANDIDATE_VERIFIED --> MITIGATED: policy authorized and activated
    CANDIDATE_VERIFIED --> AWAITING_RUNTIME_REVIEW: policy mismatch or disabled
    CANDIDATE_VERIFIED --> AWAITING_PUBLICATION_AUTHORIZATION: no draft authorization
    CANDIDATE_VERIFIED --> GITHUB_NOT_CONFIGURED: no repository or token
    CANDIDATE_VERIFIED --> CLIENT_TARGET_REQUIRED: no declarative client
    MITIGATED --> PR_OPEN: permanent PR created
    AWAITING_RUNTIME_REVIEW --> PR_OPEN: permanent PR created
    AWAITING_PUBLICATION_AUTHORIZATION --> PR_OPEN: operator authorizes
    PR_OPEN --> PR_MERGED: merged receipt
    PR_MERGED --> DEPLOYED: successful deployment for same sha
    DEPLOYED --> VERIFYING: probes authorized
    VERIFYING --> VERIFY_FAILED: probe or release mismatch
    VERIFYING --> RETIRING: samples passed
    RETIRING --> RESOLVED: adapter stopped, route DIRECT
    VERIFY_FAILED --> VERIFYING: retry after fixing deployment
```

### 7.3 Connection routing

```mermaid
stateDiagram-v2
    [*] --> MONITORING
    MONITORING --> DEGRADED: failure recorded
    DEGRADED --> MONITORING: later success with no active repair
    DEGRADED --> MONITORING: bridge activated, traffic translated
    MONITORING --> MONITORING: bridge retired, route DIRECT
```

`route.mode` is the traffic switch; `state` is health. They move independently, so
a healthy connection can still be routed through a temporary bridge.

### 7.4 Migration and jobs

```mermaid
stateDiagram-v2
    [*] --> DRAFT
    DRAFT --> DIFF_READY: analyze
    DIFF_READY --> MAPPING_PROPOSED: propose
    MAPPING_PROPOSED --> NEEDS_REVIEW: review-required issues
    NEEDS_REVIEW --> APPROVED_FOR_BUILD: approvals for exact fingerprint
    MAPPING_PROPOSED --> APPROVED_FOR_BUILD: no issues
    APPROVED_FOR_BUILD --> BUILDING: build
    BUILDING --> READY_FOR_APPROVAL: tests passed
    BUILDING --> TEST_FAILED: tests failed
    READY_FOR_APPROVAL --> ACTIVE: activate exact artifact hash
    ACTIVE --> PUBLISHING: export patch / PR
    PUBLISHING --> RETRY_REQUIRED: remote failure
```

Jobs are `QUEUED → RUNNING → PASSED | FAILED`, persisted so an API restart does
not lose the record of what ran.

---

## 8. HTTP surface

Operator routes (bearer `OPERATOR_TOKEN`):

| Method | Path | Purpose |
| --- | --- | --- |
| GET/POST | `/api/v1/connections` | list / register integrations |
| GET | `/api/v1/connections/{id}` | connection + example request + gateway path |
| POST | `/api/v1/connections/{id}/access` | re-issue scoped gateway credential |
| PUT | `/api/v1/connections/{id}/fixtures` | replace verification fixtures |
| PUT | `/api/v1/connections/{id}/target` | bind provider target path |
| PUT | `/api/v1/connections/{id}/draft-automation` | authorize automatic draft PRs |
| POST | `/api/v1/connections/{id}/route` | select an active bridge |
| POST | `/api/v1/connections/{id}/direct` | return routing to direct |
| GET | `/api/v1/connections/{id}/audit` | append-only action history |
| GET | `/api/v1/incidents` | list incidents |
| GET | `/api/v1/incidents/{id}` | incident + candidate + mapping + evidence |
| POST | `/api/v1/incidents/{id}/retry` | retry investigation |
| GET | `/api/v1/incidents/{id}/patch` | download the real code patch |
| GET | `/api/v1/incidents/{id}/pr-preview` | preview PR files (no remote write) |
| POST | `/api/v1/incidents/{id}/publish` | publish draft PR when authorized |
| GET | `/api/v1/migrations/{id}` and sub-routes | spec-to-spec workflow, builds, replay, export |

Application data routes (scoped `cbg.` token, POST only):

| Path | Purpose |
| --- | --- |
| `/api/v1/gateway/{connectionId}{sourcePath}` | monitored call, translated when a bridge is active |
| `/api/v1/native/{connectionId}{targetPath}` | permanent-client path, requires `X-ContractBridge-Client-Revision` |

Webhook routes (HMAC only): `/api/v1/hooks/sentry`, `/api/v1/hooks/github`.
Ingress (no operator credential): `/{buildId}/{path}` on port 18080.
Web server proxies: `/api/bridge/**` (session cookie), `/api/shop/**` (shop token).

---

## 9. Configuration that changes behaviour

| Variable | Effect |
| --- | --- |
| `OPERATOR_TOKEN` | Operator bearer; also signs the web session cookie (min 24 chars) |
| `GATEWAY_SIGNING_SECRET` | Derives `cbg.` connection tokens; rotation invalidates all |
| `UPSTREAM_ALLOWLIST` | Origins an integration may register |
| `INTEGRATIONS_FILE`, `INTEGRATIONS_ROOT` | Boot-time registrations, confined to root |
| `OLLAMA_URL`, `OLLAMA_MODEL` | Inference endpoint and model identity |
| `DUAL_REPAIR` | Use the Temporal dual lifecycle instead of single repair |
| `AUTO_MITIGATE` | Permit policy-matched automatic runtime activation |
| `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` | Durable workflow connection |
| `INCIDENT_TRIGGER` | `gateway` starts from observed failure; `sentry` waits for webhook |
| `SENTRY_WEBHOOK_SECRET`, `GITHUB_WEBHOOK_SECRET` | HMAC verification |
| `GITHUB_REPOSITORY`, `GITHUB_TOKEN`, `GITHUB_BASE_BRANCH` | Draft PR target |
| `DEPLOYMENT_ENVIRONMENT` | Environment a deployment receipt must match |
| `ALLOW_SYNTHETIC_PROBES`, `VERIFICATION_ORIGINS`, `VERIFICATION_CREDENTIALS` | Authorize bypass probes and which app/token may be called |
| `VERIFICATION_SAMPLES`, `VERIFICATION_INTERVAL` | Required healthy samples (2–10) and spacing |
| `ARTIFACT_ROOT`, `DATABASE_URL` | Storage locations |

Hard limits: Python 3.14 only (startup refuses otherwise), 1 MB upstream/ingress
bodies, 32 KB prompt context, 128 KB model response, 10 s upstream timeout, 180 s
model timeout, 300 s sandbox timeout, 12 min per workflow phase.

---

## 10. Failure semantics

| Failure | Result | Guarantee |
| --- | --- | --- |
| Invalid client payload | 422 before any upstream call | provider never sees it |
| Upstream 4xx/5xx or schema drift | 502 + `incidentId`, `requestRetried: false` | one attempt only |
| Timeout / connection error | 504 / 502, classified separately | not treated as contract drift |
| Same failure repeated | same incident, `occurrences` increments | no duplicate model work |
| Provider spec unchanged | `NO_CONTRACT_CHANGE` | no invented mapping |
| Unsupported mapping | `BLOCKED` | approval cannot override |
| Missing fixtures | `NEEDS_FIXTURES` | no untested candidate |
| Sandbox timeout | `FAILED`, retry available | no unverified artifact accepted |
| Provider changes mid-repair | `Conflict` before approval/publish/activate | mapping never applied to a different contract |
| Policy hash mismatch | `REVIEW_REQUIRED` | no automatic activation |
| GitHub baseline differs | `Conflict`, no branch written | no clobbering application code |
| Merge without deployment | bridge stays `ACTIVE` | merge alone retires nothing |
| Failed bypass probe | `VERIFY_FAILED`, bridge stays `ACTIVE` | no premature cleanup |
| Temporal unavailable | `WORKFLOW_UNAVAILABLE`, incident retained | retry does not lose the failure |

---

## 11. What each artifact proves

| Evidence | Proves | Does not prove |
| --- | --- | --- |
| Unit tests with mock transport | request construction and validation logic | a remote service was reached |
| Reference fixture plan | interpreter correctness | model quality |
| Gemma provenance (model, tokens, latency, context hash) | a real local inference produced that proposal | the mapping is semantically right |
| Isolated sandbox report with `docker inspect` fields | code compiled and tests passed under stated isolation | production compatibility |
| Draft PR URL | a remote branch and PR exist | the fix is deployed |
| Deployment receipt | a deployment event named that commit | the app works natively |
| Bypass probes | the deployed commit works without translation | future versions will |
