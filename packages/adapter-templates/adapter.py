"""Generated from trusted templates. Mapping data lives in bundle.json."""
import json
import os
from pathlib import Path

from bridge_core.runtime import create_proxy

bundle = json.loads((Path(__file__).parent / "bundle.json").read_text())
app = create_proxy(bundle, os.environ["UPSTREAM_BASE_URL"], os.environ["BRIDGE_ALLOWED_UPSTREAMS"].split(","),
                   inspect_token=os.getenv("BRIDGE_INSPECT_TOKEN", ""))
