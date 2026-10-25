"""Prepare private core credentials and an optional source-map upload secret."""

import os
import secrets
from pathlib import Path

from prepare_sentry import main as prepare_sentry

root = Path(__file__).resolve().parents[1]
path = root / ".env"
text = path.read_text() if path.exists() else ""
keys = {line.partition("=")[0]: line.partition("=")[2] for line in text.splitlines() if "=" in line}
for key in ("OPERATOR_TOKEN", "GATEWAY_SIGNING_SECRET", "POSTGRES_PASSWORD"):
    if not keys.get(key):
        text = "\n".join(line for line in text.splitlines() if not line.startswith(key + "=")) + "\n"
        text += key + "=" + secrets.token_urlsafe(32) + "\n"
path.write_text(text.lstrip("\n"))
os.chmod(path, 0o600)
prepare_sentry()
print("Core credentials configured in ignored .env")
