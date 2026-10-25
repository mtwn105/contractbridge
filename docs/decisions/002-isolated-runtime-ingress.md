# Isolated adapter network and UUID ingress

Docker verification showed that adapters on an internal-only network should not
publish host ports directly. Keep generated adapters on the fixed
`contractbridge_adapters` network and expose only a small trusted ingress attached
to both the control and adapter networks.

Ingress accepts UUID-addressed adapter routes and forwards bytes/status without
retrying POSTs, forwarding ContractBridge credentials, or exposing private
inspection headers. It contains no provider aliases or synthetic baseline routes;
examples are separate services in an optional Compose overlay.

The worker selects adapter images, mounts, upstreams, and network. Models and API
callers cannot choose Docker commands or arbitrary networks. Exported adapters
remain standalone applications and do not require this local ingress.

Ollama, PostgreSQL, Temporal, and generated adapter ports remain internal in the
base Compose deployment.
