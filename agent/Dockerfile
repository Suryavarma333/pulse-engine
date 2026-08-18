FROM python:3.11-slim AS agent-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY agent ./agent
COPY common ./common
COPY db ./db
COPY demo_app ./demo_app
COPY load_tests ./load_tests

RUN python -m pip wheel --wheel-dir /wheels .

FROM agent-builder AS load-builder
RUN python -m pip wheel --wheel-dir /load-wheels ".[load]"

FROM python:3.11-slim AS agent

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system pulse \
    && useradd --system --gid pulse --create-home --home-dir /home/pulse pulse

COPY --from=agent-builder /wheels /wheels
RUN python -m pip install /wheels/* \
    && rm -rf /wheels

USER pulse
EXPOSE 8100

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=2).read()"]

CMD ["uvicorn", "agent.app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8100"]

FROM python:3.11-slim AS load

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system pulse \
    && useradd --system --gid pulse --create-home --home-dir /home/pulse pulse

COPY --from=load-builder /load-wheels /wheels
RUN python -m pip install /wheels/* \
    && rm -rf /wheels

COPY --chown=pulse:pulse load_tests ./load_tests
COPY --chown=pulse:pulse scripts ./scripts

USER pulse

ENTRYPOINT ["python", "load_tests/scripts/run_scenario.py"]
