"""Trusted container entrypoint. No network, source input read-only, output scoped per build."""
import compileall
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

source = Path("/work/project")
shutil.copytree("/input", source)
started = time.monotonic()
compiled = compileall.compile_dir(source, quiet=2)
stages = [{"name": "Python 3.14 compilation", "passed": compiled, "durationMs": round((time.monotonic() - started) * 1000)}]
if compiled:
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "pytest_asyncio.plugin", "--asyncio-mode=auto",
                             "-q", "--tb=no", "--junitxml=/output/tests.xml"], cwd=source,
                            env={**os.environ, "PYTHONPATH": str(source), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                            timeout=120, capture_output=True, text=True)
    stages.append({"name": "Fixture and HTTP contract verification", "passed": result.returncode == 0,
                   "durationMs": round((time.monotonic() - started) * 1000)})
Path("/output/stages.json").write_text(json.dumps({"stages": stages, "pythonVersion": sys.version.split()[0]}))
raise SystemExit(0 if all(s["passed"] for s in stages) else 1)
