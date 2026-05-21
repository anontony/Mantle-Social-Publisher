FROM mcr.microsoft.com/playwright/python:v1.54.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV RUNTIME_DIR=/data
ENV PLAYWRIGHT_HEADLESS=1

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
