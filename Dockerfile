FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg strips metadata from attached video clips; without it clips can't be approved.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

USER app
VOLUME /data

ENV PIP_RELEASE_STORE=/data/state.sqlite \
    PIP_RELEASE_HEARTBEAT=/data/heartbeat

ENTRYPOINT ["pip-release"]
CMD ["watch"]
