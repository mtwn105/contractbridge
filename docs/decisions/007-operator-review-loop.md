# Operator review instead of per-integration mitigation hashes

## Context

Automatic mitigation previously required an integration-specific policy file that
pinned the old contract, new contract, fixtures, and normalized mapping behavior.
That made the generic core depend on example-specific data and required a new hash
file for every legitimate contract change. It also failed on equivalent model
output that differed only in non-semantic ordering.

## Decision

Remove the mitigation-policy files and hash checks. Every verified candidate stops
at `READY_FOR_REVIEW` in the control plane. The operator sees the mapping, fixture
evidence, and exact candidate artifact hash, then either:

- **approves** it, which records the operator comment as the semantic review,
  builds from the approved inputs, and routes traffic through the exact artifact;
- **rejects** it with a comment, which is stored in the incident audit and passed
  to the next Gemma proposal as `operatorReviewFeedback`. The new mapping is
  validated and tested again before it returns to review.

No model output bypasses schema validation, fixture verification, isolated
candidate tests, or exact artifact approval.

## Consequences

- The core has no integration-specific allowlists or pinned behavior hashes.
- Activation always has a recorded human decision for the current candidate.
- Revisions are driven by operator feedback rather than model self-correction.
- A rejected candidate cannot be silently reactivated; any new candidate must
  pass verification and be reviewed again.
