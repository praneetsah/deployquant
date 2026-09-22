# The image the compose file runs: python 3.12, deployquant with the live
# extra, and one broker plugin.
#
# BROKER_PLUGIN is a pip name (deployquant-webull, deployquant-schwab, or
# nothing for the bundled Alpaca adapters). DEPLOYQUANT_VERSION pins the
# engine; empty takes the latest release.
FROM python:3.12-slim

ARG BROKER_PLUGIN=
ARG DEPLOYQUANT_VERSION=

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      "deployquant[live]${DEPLOYQUANT_VERSION:+==${DEPLOYQUANT_VERSION}}" \
    && if [ -n "$BROKER_PLUGIN" ]; then \
         pip install --no-cache-dir "$BROKER_PLUGIN"; \
       fi

RUN useradd -m -u 1500 dq && mkdir -p /srv/data /srv/live-data \
    && chown -R dq /srv
USER dq
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
ENTRYPOINT []
CMD ["dqengine", "--help"]
