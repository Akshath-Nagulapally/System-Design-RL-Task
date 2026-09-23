FROM golang:1.25.1-alpine3.22 AS go

FROM docker:28.0.4-dind

ARG K3D_VERSION=v5.8.3
ARG KUBECTL_VERSION=v1.33.4

RUN apk add --no-cache \
    bash \
    build-base \
    busybox-extras \
    ca-certificates \
    coreutils \
    curl \
    git \
    iproute2 \
    jq \
    openrc \
    openssl \
    tar \
    util-linux

COPY --from=go /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}"

RUN mkdir -p /run/openrc /etc/rancher/k3s && \
    touch /run/openrc/softlevel && \
    printf 'snapshotter: native\n' > /etc/rancher/k3s/config.yaml

RUN set -eux; \
    case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) exit 1 ;; esac; \
    curl -fsSL "https://github.com/k3d-io/k3d/releases/download/${K3D_VERSION}/k3d-linux-${arch}" -o /usr/local/bin/k3d; \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" -o /usr/local/bin/kubectl; \
    chmod +x /usr/local/bin/k3d /usr/local/bin/kubectl

ENV DOCKER_TLS_CERTDIR=""
WORKDIR /app

COPY spec.md /app/spec.md
COPY seed/kv.jsonl /seed/kv.jsonl
RUN chmod 0444 /app/spec.md /seed/kv.jsonl
