FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

ARG CODEX_VERSION=0.144.4
ENV NPM_CONFIG_UPDATE_NOTIFIER=false

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && npm install --global --omit=dev "@openai/codex@${CODEX_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/node_modules/npm \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx \
    && dpkg --purge --force-depends --force-remove-essential perl-base
RUN codex --version

USER 1000:1000
WORKDIR /workspace
CMD ["codex"]
