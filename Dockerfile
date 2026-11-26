# upgrade-impact as a container: the CLI, the MCP server (stdio) and the
# OpenTelemetry extra. Build:  docker build -t upgrade-impact .
# Run on the current checkout:
#   docker run --rm -v "$PWD:/work" -e GITHUB_TOKEN upgrade-impact check <PR URL>

FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir --disable-pip-version-check build==1.2.2.post1
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.12-slim
# git: detect whether /work is a checkout of the PR's repository, and clone for `eval`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /dist /dist
RUN wheel="$(ls /dist/*.whl)" \
    && pip install --no-cache-dir --disable-pip-version-check "${wheel}[otel]" \
    && rm -rf /dist
RUN useradd --create-home --uid 10001 app \
    && git config --system --add safe.directory '*'
USER app
ENV PYTHONUNBUFFERED=1 \
    UPGRADE_IMPACT_CACHE_DIR=/home/app/.cache/upgrade-impact
WORKDIR /work
ENTRYPOINT ["upgrade-impact"]
CMD ["--help"]
