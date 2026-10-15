# Match Safactory's PJLab runtime for Linux/AMD64 deployments.
ARG BASE_IMAGE=registry.h.pjlab.org.cn/ailab/ml-base:22.04-pjlab-20260323
FROM ${BASE_IMAGE}

# RJob worker-init writes SSH tools and platform files under /usr, /etc and /dev.
USER root

# Keep the same laboratory proxy configuration as Dockerfile.safactory.
ARG HTTP_PROXY=http://httpproxy-headless.kubebrain.svc.pjlab.local:3128
ARG HTTPS_PROXY=http://httpproxy-headless.kubebrain.svc.pjlab.local:3128
ARG NO_PROXY=localhost,127.0.0.1

ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    no_proxy=${NO_PROXY} \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PROXY_LISTEN_HOST=0.0.0.0 \
    PROXY_LISTEN_PORT=9000

RUN mkdir -p /app
WORKDIR /app
COPY proxy.py /app/proxy.py

# The base already supplies Python; this proxy needs no third-party packages.
RUN python -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'

# RJob uses sh -lc, and the base's brain-ulimit.sh profile contains Bash syntax.
RUN test -x /bin/bash \
    && ln -sf /bin/bash /bin/sh \
    && /bin/sh -lc 'true'

ARG APP_ENV=production
ENV APP_ENV=${APP_ENV} \
    PYTHONPATH=/app \
    PATH=/app/bin:${PATH}

EXPOSE 9000/tcp

# As in Safactory, the platform supplies the startup command:
# python /app/proxy.py (GATEWAY_IP and GATEWAY_PORT come from environment).
ENTRYPOINT []
CMD []
