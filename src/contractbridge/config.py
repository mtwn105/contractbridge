from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    artifacts: Path
    database_url: str
    operator_token: str
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "gemma3:4b"
    sentry_dsn: str = ""
    runner_timeout: int = 300
    upstream_allowlist: tuple[str, ...] = ()
    gateway_secret: str = ""
    integrations_file: str = ""
    integrations_root: str = ""
    github_repository: str = ""
    github_token: str = ""
    github_base: str = "main"
    sentry_org: str = ""
    dual_repair: bool = False
    temporal_address: str = ""
    temporal_namespace: str = "default"
    sentry_webhook_secret: str = ""
    github_webhook_secret: str = ""
    incident_trigger: str = "gateway"
    deployment_environment: str = "staging"
    verification_samples: int = 3
    verification_interval: float = 2.0
    allow_synthetic_probes: bool = False
    verification_origins: tuple[str, ...] = ()
    verification_credentials: tuple[str, ...] = ()

    @classmethod
    def environment(cls):
        if sys.version_info[:2] != (3, 14):
            raise RuntimeError("ContractBridge requires Python 3.14")
        root = Path(os.getenv("CONTRACTBRIDGE_ROOT", Path(__file__).resolve().parents[2])).resolve()
        token = os.getenv("OPERATOR_TOKEN", "")
        if len(token) < 24:
            raise RuntimeError("Set OPERATOR_TOKEN to at least 24 characters; bootstrap generates it")

        def origins(name):
            return tuple(v.strip().rstrip("/") for v in os.getenv(name, "").split(",") if v.strip())

        return cls(
            root=root,
            artifacts=Path(os.getenv("ARTIFACT_ROOT", root / ".local/artifacts")),
            database_url=os.getenv("DATABASE_URL", "sqlite:///" + str(root / ".local/contractbridge.db")),
            operator_token=token,
            ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
            model=os.getenv("OLLAMA_MODEL", "gemma3:4b"),
            sentry_dsn=os.getenv("SENTRY_DSN", ""),
            upstream_allowlist=origins("UPSTREAM_ALLOWLIST"),
            gateway_secret=os.getenv("GATEWAY_SIGNING_SECRET", ""),
            integrations_file=os.getenv("INTEGRATIONS_FILE", ""),
            integrations_root=os.getenv("INTEGRATIONS_ROOT", ""),
            github_repository=os.getenv("GITHUB_REPOSITORY", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            github_base=os.getenv("GITHUB_BASE_BRANCH", "main"),
            sentry_org=os.getenv("SENTRY_ORG", ""),
            dual_repair=os.getenv("DUAL_REPAIR", "0") == "1",
            temporal_address=os.getenv("TEMPORAL_ADDRESS", ""),
            temporal_namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
            sentry_webhook_secret=os.getenv("SENTRY_WEBHOOK_SECRET", ""),
            github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
            incident_trigger=os.getenv("INCIDENT_TRIGGER", "gateway"),
            deployment_environment=os.getenv("DEPLOYMENT_ENVIRONMENT", "staging"),
            verification_samples=int(os.getenv("VERIFICATION_SAMPLES", "3")),
            verification_interval=float(os.getenv("VERIFICATION_INTERVAL", "2")),
            allow_synthetic_probes=os.getenv("ALLOW_SYNTHETIC_PROBES", "0") == "1",
            verification_origins=origins("VERIFICATION_ORIGINS"),
            verification_credentials=tuple(
                v.strip() for v in os.getenv("VERIFICATION_CREDENTIALS", "").split(",") if v.strip()
            ),
        )
