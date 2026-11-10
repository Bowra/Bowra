# Bowhead sandbox-executor image (sandbox-hardening 2026-06-17, Phase 3 de-DooD).
# The ONLY container that gets /var/run/docker.sock. Minimal: docker CLI + python3
# (stdlib-only app with no pip dependencies; base OS and Docker CLI still need security updates).
#
# `docker:cli` already ships the docker client. Add python3 for app.py.
# The Tencent mirror returned IO errors and the official CDN stalled during this
# build; keep apk signature checks enabled while using the reachable Aliyun mirror.
ARG DOCKER_REGISTRY=docker.m.daocloud.io/
FROM ${DOCKER_REGISTRY}docker:29.9.3-cli

RUN set -eux; \
    sed -i 's#https\?://dl-cdn.alpinelinux.org#https://mirrors.aliyun.com#g' /etc/apk/repositories || true; \
    apk add --no-cache python3

COPY app.py /opt/bowhead/sandbox-executor/app.py
COPY document_runs.py /opt/bowhead/sandbox-executor/document_runs.py

# Run as root: the executor must talk to the docker socket (group/uid varies per
# host; the security boundary here is the SINGLE narrow /run endpoint + hardcoded
# flags, not in-container uid). It never runs untrusted code itself — it only
# spawns the sandbox container, which IS unprivileged + gVisor-isolated.
EXPOSE 8091
CMD ["python3", "/opt/bowhead/sandbox-executor/app.py"]
