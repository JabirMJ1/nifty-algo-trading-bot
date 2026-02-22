FROM python:3.11-slim

WORKDIR /app

# Install system dependencies including TA-Lib C library
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    wget \
    && wget -q http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz \
    && tar -xzf ta-lib-0.4.0-src.tar.gz \
    && cd ta-lib && ./configure --prefix=/usr && make && make install && cd .. \
    && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz \
    && apt-get purge -y wget make \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (numpy first, needed to build ta-lib)
COPY requirements.txt .
RUN pip install --no-cache-dir numpy==1.26.3 && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create log directory
RUN mkdir -p logs data

# Run as non-root user
RUN useradd -m botuser
USER botuser

CMD ["python", "main.py"]
