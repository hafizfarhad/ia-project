# One image for all Python services; each compose service picks its command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Least privilege inside the container too. models/ and data/ are writable by the app user
# (a fresh named volume inherits this ownership).
RUN useradd --create-home --uid 1000 app && mkdir -p /app/models /app/data && chown app:app /app/models /app/data
USER app
