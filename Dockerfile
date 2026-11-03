# The uro-server image (D-94): `uro serve` and the rest of the `uro` CLI, built from the locked
# workspace. The store stays external (D-43: Postgres + pgvector is the one store) — point it at
# one with URO_DATABASE_URL, and pass URO_SECRET_KEY for the model registry's credentials. Nothing
# secret is baked in (.dockerignore keeps .env, uro.toml and local data out).
#
#   docker build -t uro-server .
#   docker run --rm -p 8000:8000 -e URO_DATABASE_URL=postgresql://uro:uro@db:5432/uro uro-server
FROM python:3.12-slim

RUN pip install --no-cache-dir "uv==0.13.4"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN uv sync --frozen --all-packages --no-dev

COPY worlds ./worlds
RUN useradd --create-home --uid 10001 uro && chown -R uro /app
USER uro
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
ENTRYPOINT ["uro"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
