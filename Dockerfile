FROM python:3.12-slim

WORKDIR /app

# Dépendances système : AutoTrace + build tools pour vtracer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    cargo \
    rustc \
    autotrace \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 3000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]
