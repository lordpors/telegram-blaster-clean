FROM python:3.11-slim

# System dependencies (git, chromium, curl untuk install node)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    chromium \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && npm --version

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Node deps buat wa_service (di build time, sekali aja)
COPY wa_service/package.json ./wa_service/
RUN cd wa_service && npm install --omit=dev --no-audit --no-fund

# Copy sisa aplikasi
COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1"]
