# Generated ContractBridge adapter

Requires Python 3.14. Install requirements.txt and run:

```sh
export UPSTREAM_BASE_URL=http://upstream-v2:8080
export BRIDGE_ALLOWED_UPSTREAMS=http://upstream-v2:8080
python -m uvicorn adapter:app --host 0.0.0.0 --port 8080
```

The model is never called at runtime. Request and response schemas are validated.
Unmapped upstream statuses return 502; timeouts return 504. POSTs are not retried.
Only the approved success response is translated. Other error shapes are unsupported.
Typed dataclasses are exported in dtos.py; schema constraints are enforced at runtime.
