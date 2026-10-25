"""Prepare only the optional shopping example; core registration stays generic."""

import os
import secrets
import sys
from pathlib import Path


def main():
    ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    from contractbridge.integrations import gateway_token
    from examples.shopping.settings import CONNECTION_ID

    values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in (ROOT / ".env").read_text().splitlines()
        if "=" in line
    }
    file = ROOT / ".local/examples.env"
    previous = (
        {
            line.partition("=")[0]: line.partition("=")[2]
            for line in file.read_text().splitlines()
            if "=" in line
        }
        if file.exists()
        else {}
    )
    token = previous.get("SHOP_API_TOKEN") or values.get("SHOP_API_TOKEN") or secrets.token_urlsafe(32)
    file.write_text(
        "SHOP_API_TOKEN="
        + token
        + "\nEXAMPLE_APPLICATION_TOKEN="
        + token
        + "\nSHOP_GATEWAY_TOKEN="
        + gateway_token(values["GATEWAY_SIGNING_SECRET"], CONNECTION_ID)
        + "\n"
    )
    os.chmod(file, 0o600)
    provider = ROOT / "examples/provider/provider.json"
    if not provider.exists():
        provider.write_bytes((provider.parent / "default.json").read_bytes())
    print("Optional shopping example credentials configured locally")


if __name__ == "__main__":
    main()
