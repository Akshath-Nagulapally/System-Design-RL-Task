FROM docker:27-dind

RUN apk add --no-cache \
    bash \
    ca-certificates \
    coreutils \
    curl \
    git \
    go \
    iproute2 \
    jq \
    openssl \
    tar \
    util-linux

ENV DOCKER_TLS_CERTDIR=""
WORKDIR /app

COPY spec.md /app/spec.md
COPY seed/kv.jsonl /seed/kv.jsonl
RUN chmod 0444 /app/spec.md /seed/kv.jsonl
