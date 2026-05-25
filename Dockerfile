FROM python:3.11-slim

# wget/curl are required by playwright install --with-deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caches unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser + all its system dependencies in one step
RUN playwright install --with-deps chromium

# Copy application code
COPY . .

# Headless is always true in the container (no display available)
ENV HEADLESS=true
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV PORT=10000

EXPOSE 10000

CMD ["python", "app.py"]
