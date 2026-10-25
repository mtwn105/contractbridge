# syntax=docker/dockerfile:1
FROM python:3.14.4-slim
WORKDIR /app
COPY requirements.lock ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements.lock; else pip install --no-cache-dir -r requirements.lock; fi
COPY src ./src
COPY examples ./examples
COPY packages ./packages
ARG APP_COMMIT_SHA=development
ENV APP_COMMIT_SHA=$APP_COMMIT_SHA PYTHONPATH=/app/src CONTRACTBRIDGE_ROOT=/app PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
CMD ["python","-m","uvicorn","examples.shopping.api:create_app","--factory","--host","0.0.0.0","--port","8080","--no-access-log"]
