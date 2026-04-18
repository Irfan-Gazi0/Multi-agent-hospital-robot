FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project source
COPY . .

# Unbuffered output so logs appear immediately
ENV PYTHONUNBUFFERED=1

# Default: run the scenario demo
CMD ["python", "test_runner.py"]
