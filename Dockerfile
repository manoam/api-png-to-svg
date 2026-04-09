FROM python:3.12-slim

WORKDIR /app

# Build tools pour vtracer + dépendances pour autotrace
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    cargo \
    rustc \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    git \
    make \
    autoconf \
    automake \
    libtool \
    pkg-config \
    libglib2.0-dev \
    intltool \
    imagemagick \
    libmagickcore-dev \
    && rm -rf /var/lib/apt/lists/*

# Compiler autotrace depuis les sources
RUN git clone https://github.com/autotrace/autotrace.git /tmp/autotrace \
    && cd /tmp/autotrace \
    && ./autogen.sh \
    && ./configure \
    && make \
    && make install \
    && ldconfig \
    && rm -rf /tmp/autotrace

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 3000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]
