FROM python:3.11-slim

# Set system environment optimizations
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Install required system components for network health verification
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 2. INLINE DEPENDENCY INSTALLATION
# Installs core web layers, asynchronous AWS S3 clients, and Postgres pooled drivers directly
RUN pip install --no-cache-dir \
    fastapi>=0.100.0 \
    uvicorn>=0.22.0 \
    aioboto3>=13.0.0 \
    aiobotocore>=2.11.0 \
    sqlalchemy>=2.0.0 \
    asyncpg>=0.28.0

# 3. Explicitly copy only the application assets
COPY license_server.py create_tables.py .

# Expose port matching your Fargate Application Load Balancer setup
EXPOSE 8000

# Default entry point execution layer (Launches your API behind CloudFront)
CMD ["uvicorn", "license_server:app", "--host", "0.0.0.0", "--port", "8000"]
