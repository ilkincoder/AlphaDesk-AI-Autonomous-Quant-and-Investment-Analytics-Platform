FROM python:3.12-slim

# Don't write .pyc files (the source directory is bind-mounted in development),
# and don't buffer stdout/stderr, so log lines appear immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies are installed in their own layer, before the application code is
# copied in. Editing app code therefore does not invalidate this slow layer.
# The cache mount keeps downloaded wheels available to later builds.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY app ./app
# Alembic files, so migrations can be run from a container that is not relying on
# the development bind mount.
COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
