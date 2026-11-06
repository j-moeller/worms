FROM node:22-bookworm-slim AS node
FROM python:3.14-bookworm
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_PATH=/opt/browser-tests/node_modules
RUN apt-get update && apt-get install -y --no-install-recommends \
      postgresql nginx openssl git nodejs npm nftables iproute2 tcpdump \
      shellcheck systemd sudo \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/test-requirements.txt
RUN pip install --no-cache-dir -r /tmp/test-requirements.txt gunicorn
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN npm install --prefix /opt/browser-tests playwright \
    && /opt/browser-tests/node_modules/.bin/playwright install --with-deps chromium firefox webkit \
    && rm -rf /var/lib/apt/lists/* /root/.npm
WORKDIR /repo
CMD ["python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache"]
# Cover the optional Linux/infrastructure checks too, without changing the host.
ARG TARGETARCH
ARG OPENTOFU_VERSION=1.13.2
COPY docker/install_tofu.py /tmp/install_tofu.py
RUN apt-get update && apt-get install -y --no-install-recommends rsync \
    && OPENTOFU_VERSION=$OPENTOFU_VERSION TARGETARCH=$TARGETARCH python /tmp/install_tofu.py \
    && rm -rf /var/lib/apt/lists/* /tmp/install_tofu.py
RUN git config --system --add safe.directory /repo
