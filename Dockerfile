FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip==24.0 && \
    pip install -r requirements.txt

COPY . .
RUN pip install --no-deps -e .

EXPOSE 8000

CMD ["strata", "server", "start", "--host", "0.0.0.0", "--port", "8000"]
