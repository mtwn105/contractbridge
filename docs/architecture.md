# ContractBridge architecture

> For the one-page, blog-friendly overview and hero diagram, see
> [architecture overview](architecture-overview.md). This page keeps the detailed
> deployment and trust-boundary view.

This is the current OpenAPI-based system, checked against the source and Compose
configuration. It follows the generic dual-repair and operator-review decisions
([006](decisions/006-generic-dual-repair.md),
[007](decisions/007-operator-review-loop.md)). The spec-optional / observed-contract
mode discussed in the saved session is a proposal, not an implemented component.
These diagrams describe architecture; they are not evidence of a live inference,
successful container build, or deployment.

## System overview

Solid arrows show calls or artifact flow; dotted arrows show background signals,
evidence, or control actions. The gateway and management API share the
`control-plane` service, but only background investigation invokes the model.

```mermaid
flowchart TB
    OP["Operator"] --> WEB["Next.js console<br/>operator token held server-side"]
    APP["Registered application<br/>scoped gateway token"] --> GW

    subgraph CP["control-plane service · Python / Starlette"]
        API["Management API<br/>integrations, incidents, review"]
        GW["Gateway<br/>validate contract · choose route"]
        REPAIR["Repair pipeline<br/>OpenAPI + oasdiff · validate mapping"]
        GEN["Trusted generator<br/>fixed templates + interpreter"]
        PUB["Permanent client patch<br/>registered python-declarative target"]
        API --> REPAIR
        REPAIR --> GEN
        REPAIR --> PUB
        GW -.->|"contract failure · incident"| REPAIR
    end

    WEB --> API
    GW -->|"DIRECT · one upstream attempt"| UP["Allowed upstream API"]
    GW -->|"BRIDGE · subsequent requests"| AD["Approved adapter<br/>deterministic request / response mapping"]
    AD --> UP
    REPAIR -->|"fetch current OpenAPI"| UP
    REPAIR <-->|"constrained JSON proposal"| AI["Local Ollama / Gemma<br/>data only · outside request path"]

    API <--> DB[("PostgreSQL<br/>state · revisions · review audit")]
    REPAIR <--> DB
    API -->|"start / signal"| TEMP["Temporal<br/>durable workflow state"]
    TEMP <--> ORCH["Orchestrator<br/>phase activities"]
    ORCH --> API

    GEN --> ART[("Artifacts volume<br/>frozen inputs · hashes · jobs · reports")]
    ART <--> WORK["Trusted Docker worker<br/>sole holder of Docker socket"]
    WORK --> SB["Verification container<br/>network none · compile + pytest"]
    SB -->|"test report"| ART
    WORK -.->|"start approved build + health check"| AD
    API -.->|"approval + passing build + exact artifact"| WORK

    PUB -->|"downloadable patch; optional draft PR"| GH["GitHub<br/>review · merge · deploy externally"]
    GH -.->|"signed merge / deployment evidence"| API
    API -.->|"authorized synthetic native probes"| DEP["Deployed registered application"]
    DEP -->|"native client call without translation"| UP
    API -.->|"matching deployment + passing probes: retire"| AD

    GW -.->|"IDs / metadata only"| SEN["Sentry"]
    SEN -.->|"signed project-bound alert"| API
    LOCAL["Local adapter caller"] --> ING["UUID-addressed ingress"]
    ING --> AD
```

The failed business POST is never automatically replayed. Repair restores later
requests after approval. The adapter maps both requests and responses so the old
client can continue using its expected contract.

## One mapping, two repairs

```mermaid
flowchart TD
    INPUT["Registered client OpenAPI + current upstream OpenAPI<br/>independent synthetic / sanitized fixtures"] --> DIFF["Parse, select supported operation, diff"]
    DIFF --> MODEL["Gemma proposes a declarative JSON mapping"]
    MODEL --> VALID["Schema + semantic checks<br/>verify independent fixtures"]
    TPL["Trusted templates and interpreter<br/>maintained source code"] --> RENDER
    VALID --> RENDER["Freeze generated candidate inputs"]
    RENDER --> TEST["Docker worker: isolated compile + tests<br/>record hashes and isolation report"]
    TEST --> REVIEW{"Operator reviews exact<br/>verified candidate"}
    REVIEW -->|"reject + comment"| MODEL
    REVIEW -->|"approve: record semantic review"| SHARED["Approved immutable mapping"]
    SHARED --> BUILD["Build temporary adapter from approved inputs<br/>require passing tests + exact artifact hash"]
    BUILD --> ACTIVE["Activate BRIDGE route"]
    SHARED --> PATCH["Verify permanent declarative client patch"]
    PATCH --> PR["Patch download / configured GitHub draft PR"]
    PR --> MERGE["External review and merge"]
    MERGE --> DEPLOY["Successful deployment evidence<br/>matching commit + registered environment"]
    DEPLOY --> PROBE["Authorized synthetic native probes<br/>verify deployed identity with translation bypassed"]
    ACTIVE -.->|"keep bridge while waiting"| PROBE
    PROBE -->|"passing required samples"| RETIRE["Retire exact temporary bridge"]
    PROBE -->|"failure"| KEEP["Keep temporary bridge active"]
    VALID -->|"unsupported / missing evidence"| STOP["Stop for human action"]
    TEST -->|"failed / missing verification"| STOP
```

The provider contract is checked again before approval, publication, and
activation. A stale candidate cannot proceed. Merge alone never retires a bridge;
ContractBridge does not merge or deploy the permanent patch.

## Deployment and trust boundaries

| Boundary | Components and constraints |
| --- | --- |
| Private `control` network | Web, control plane, PostgreSQL, Ollama, Temporal, orchestrator, worker, and ingress. |
| Internal `adapters` network | Dynamic adapters; control plane and ingress also join this network. The optional synthetic provider joins through the example overlay. |
| Verification container | Created by the worker with no network, read-only root, non-root user, resource limits, and no Docker socket. |
| Trusted host authority | Only the worker mounts the Docker socket; artifact jobs connect it to the control plane. |
| Loopback host ports | Console `3000`, API / gateway `8000`, UUID ingress `18080`. Database, model, Temporal, and adapter ports remain internal. |
| External integrations | Allowed upstream origins, optional Sentry, and optional GitHub; signed webhooks are bound to registered identities. |

Proposals, reference fixtures, trusted templates, and generated artifacts remain
separate evidence classes. A reference mapping is not live Gemma evidence, and a
test double is not a verified container build. Business payloads and secrets do
not belong in telemetry or demo evidence.

The synthetic shop is an optional application/provider example, not part of the
generic core. The local Temporal development server is for demos.

See [low-level design](low-level-design.md) for sequences and state machines,
[security](security.md) for authorization boundaries, and
[verification](verification.md) for evidence requirements. Implementation anchors:
[`monitoring.py`](../src/contractbridge/monitoring.py),
[`dual_repair.py`](../src/contractbridge/dual_repair.py),
[`generation.py`](../src/contractbridge/generation.py),
[`worker.py`](../ops/worker.py), and [`docker-compose.yml`](../docker-compose.yml).
