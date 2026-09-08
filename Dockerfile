# The warehouse image: Alembic migrations, ingest jobs, verify/gate scripts.
# Never started via `docker compose up` (see deploy/docker-compose.yml's
# `profiles: ["cron"]`) - only ever run on demand:
#   docker compose run --rm warehouse uv run alembic upgrade head
#   docker compose run --rm warehouse make ingest-direct
#   docker compose run --rm warehouse make verify

FROM python:3.12-slim

# `make` is this repo's documented command interface (see CLAUDE.md) - keep
# it available inside the container too, not just on a developer's machine.
RUN apt-get update && apt-get install -y --no-install-recommends make \
    && rm -rf /var/lib/apt/lists/*

# uv ships as a couple of static binaries - the officially documented way to
# get them into an image is copying straight out of astral's own image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
# Dependencies first, so an unrelated code change doesn't invalidate this
# (slow) layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"
CMD ["make", "help"]
