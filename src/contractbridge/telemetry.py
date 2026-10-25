from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

VERIFICATION_MESSAGE = "ContractBridge backend monitoring verification"


class MonitoringVerificationError(RuntimeError):
    def __init__(self):
        super().__init__(VERIFICATION_MESSAGE)


def scrub_event(event, hint=None):
    """Retain diagnostic frames and metadata; never ship application payloads."""
    for key in ("request", "user", "breadcrumbs", "extra", "logentry", "message"):
        event.pop(key, None)
    for exception in event.get("exception", {}).get("values", []):
        if exception.get("value") != VERIFICATION_MESSAGE:
            exception["value"] = "Exception details withheld by ContractBridge privacy policy"
        for frame in exception.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)
    return event


def initialize(dsn: str) -> None:
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError as exc:
        raise RuntimeError(
            "SENTRY_DSN is set but sentry-sdk is missing; install the deploy extra"
        ) from exc
    environment = os.getenv("SENTRY_ENVIRONMENT", "development")
    rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1" if environment == "production" else "1.0"))
    if not 0 <= rate <= 1:
        raise ValueError("SENTRY_TRACES_SAMPLE_RATE must be between zero and one")
    sentry_sdk.init(
        dsn=dsn,
        default_integrations=False,
        traces_sample_rate=rate,
        environment=environment,
        release=os.getenv("SENTRY_RELEASE", "contractbridge@0.1.0"),
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=scrub_event,
        before_send_transaction=scrub_event,
    )


def capture_failure(error: Exception, category: str, tags: dict[str, str] | None = None) -> None:
    """Report a caught failure through the application's already initialized SDK."""
    try:
        import sentry_sdk
    except ImportError:
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("failure.category", category)
        for key, value in (tags or {}).items():
            if key not in {"contractbridge.connection_id", "contractbridge.incident_id"}:
                raise ValueError("Telemetry tag is not allowlisted")
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError("Telemetry tag value is invalid")
            scope.set_tag(key, value)
        sentry_sdk.capture_exception(error)


class SafeSpan:
    ALLOWED = {
        "gen_ai.system",
        "gen_ai.provider.name",
        "gen_ai.operation.type",
        "gen_ai.operation.name",
        "gen_ai.conversation.id",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "prompt.version",
        "plan.hash",
        "validation.issue_count",
        "build.passed",
        "proposal.latency_ms",
        "fixture.count",
        "failure.category",
    }

    def __init__(self, span=None):
        self.span = span

    def set(self, key: str, value: Any):
        if key not in self.ALLOWED:
            raise ValueError("Telemetry attribute is not allowlisted")
        if self.span:
            self.span.set_data(key, value)


@contextmanager
def span(op: str, name: str):
    try:
        import sentry_sdk
    except ImportError:
        yield SafeSpan()
        return
    parent = sentry_sdk.get_current_span()
    context = (
        sentry_sdk.start_span(op=op, name=name) if parent else sentry_sdk.start_transaction(op=op, name=name)
    )
    with context as transaction:
        yield SafeSpan(transaction)
