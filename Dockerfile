# Palinode server image — runs the API (default) or the watcher (override CMD).
# Built and orchestrated by docker-compose.yml; see README "Running as a service".
FROM python:3.12-slim

# git: every memory save is git-committed (files-are-truth provenance).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY palinode ./palinode
RUN pip install --no-cache-dir .

COPY deploy/docker/entrypoint.sh /usr/local/bin/palinode-entrypoint
RUN chmod +x /usr/local/bin/palinode-entrypoint

# The memory dir — bind-mount your host directory here (files are the truth;
# they must survive the container).
ENV PALINODE_DIR=/data
VOLUME /data

EXPOSE 6340

ENTRYPOINT ["palinode-entrypoint"]
CMD ["uvicorn", "palinode.api.server:app", "--host", "0.0.0.0", "--port", "6340"]
