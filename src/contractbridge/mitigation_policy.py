"""Explicit semantic policy applies only to its exact contracts, fixtures, and behavior."""

from .models import check_shape
from .storage import Conflict
from .util import digest


def behavior(value):
    if isinstance(value, list):
        return [behavior(v) for v in value]
    if isinstance(value, dict):
        return {
            k: ("copy" if k == "op" and v in ("rename", "nest", "flatten") else behavior(v))
            for k, v in value.items()
            if k not in {"requiresHumanApproval", "reason", "confidence", "unresolvedQuestions"}
        }
    return value


def validate_policy(value):
    fields = {
        key: {"type": "string", "pattern": "^[a-f0-9]{64}$"}
        for key in ("oldHash", "newHash", "fixturesHash", "behaviorHash")
    }
    fields["semanticReview"] = {"type": "string", "minLength": 10, "maxLength": 4000}
    check_shape(
        {"type": "object", "additionalProperties": False, "required": list(fields), "properties": fields},
        value,
    )
    return value


def require_authorization(settings, connection, migration):
    policy = connection.get("mitigationAuthorization")
    if not settings.auto_mitigate or not policy:
        raise Conflict("Automatic mitigation requires a recorded integration-specific semantic policy")
    validate_policy(policy)
    if (
        digest(migration["specs"]["old"]) != policy["oldHash"]
        or digest(migration["specs"]["new"]) != policy["newHash"]
        or digest(migration["fixtures"]) != policy["fixturesHash"]
        or digest(behavior(migration["plan"])) != policy["behaviorHash"]
    ):
        raise Conflict("This repair exceeds the reviewed semantic policy; retain it for PR review")
    return policy
