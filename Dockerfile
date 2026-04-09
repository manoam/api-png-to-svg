FROM python:3.12-slim

WORKDIR /app

# Build tools pour vtracer (Rust)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    cargo \
    rustc \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY vectorizer.py .

EXPOSE 3000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]
