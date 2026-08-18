FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system pulse && useradd --system --gid pulse --create-home pulse

COPY pyproject.toml README.md ./
COPY common ./common
COPY db ./db
COPY demo_app ./demo_app

RUN pip install --no-cache-dir .

USER pulse
EXPOSE 8000

CMD ["uvicorn", "demo_app.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
