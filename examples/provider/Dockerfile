FROM python:3.14.4-slim
WORKDIR /app
COPY examples/demo_services.py ./demo_services.py
COPY examples/orders-v1-to-v2/openapi-v1.yaml examples/orders-v1-to-v2/openapi-v2.yaml ./contracts/
ENV CONTRACT_DIRECTORY=/app/contracts
USER 10001:10001
ENTRYPOINT ["python", "/app/demo_services.py"]
