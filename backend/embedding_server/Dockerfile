# Digest pin (issue #404 / #391) for supply-chain integrity; dependabot
# (docker ecosystem, "/backend/embedding_server") maintains updates.
# Matches the python 3.11 base used by CI and the root Dockerfile.
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY server.py .

# Expose port
EXPOSE 18080

# Run server
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "18080"]
