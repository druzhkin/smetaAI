FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir uv==0.9.27
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-editable

ENV PATH="/app/.venv/bin:${PATH}"

RUN useradd --create-home --uid 10001 tenderguard \
    && mkdir -p /app/var \
    && chown -R tenderguard:tenderguard /app/var
USER tenderguard

EXPOSE 8000
CMD ["uvicorn", "tenderguard.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
