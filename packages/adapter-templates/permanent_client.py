"""Trusted application client; generated plans remain JSON data."""

from pathlib import Path

from bridge_core.contracts import Contract, parse_spec, validate_payload
from bridge_core.engine import inspect_plan, transform
from bridge_core.models import Plan
from bridge_core.util import strict_loads

BUNDLE_NAME = "bundle.json"


class Client:
    def __init__(self, legacy_contract=None):
        self.bundle = (
            strict_loads(Path(__file__).with_name(BUNDLE_NAME).read_bytes()) if BUNDLE_NAME else None
        )
        if self.bundle:
            self.plan = Plan.parse(self.bundle["plan"])
            self.old = Contract.select(self.bundle["oldSpec"], self.plan.sourceOperation["path"])
            self.new = Contract.select(self.bundle["newSpec"], self.plan.targetOperation["path"])
            if any(not i.reviewRequired for i in inspect_plan(self.plan, self.old, self.new)):
                raise ValueError("Unsupported application integration")
        else:
            self.plan = None
            self.old = self.new = Contract.select(parse_spec(Path(legacy_contract).read_text()))

    @property
    def native(self):
        return self.plan is not None

    @property
    def repair_hash(self):
        return self.bundle["repairId"] if self.bundle else None

    @property
    def path(self):
        return self.new.path

    def encode(self, payload):
        validate_payload(self.old.request, payload)
        value = transform(payload, self.plan.requestMappings) if self.plan else payload
        validate_payload(self.new.request, value)
        return value

    def decode(self, payload, status):
        if status != self.new.status:
            raise ValueError("Unexpected provider status")
        validate_payload(self.new.response, payload)
        value = transform(payload, self.plan.responseMappings) if self.plan else payload
        validate_payload(self.old.response, value)
        return value
