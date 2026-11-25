FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip hatchling uv

COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt \
    && pip install --no-cache-dir --prefix=/install --no-deps -r requirements.txt

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir --prefix=/install --no-deps --no-build-isolation .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:$PATH"

RUN groupadd --gid 1000 smartrss \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash smartrss

COPY --from=builder /install /usr/local
WORKDIR /app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

USER 1000:1000

EXPOSE 8000

CMD ["uvicorn", "smartrss.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
