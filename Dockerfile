FROM python:3.11-slim

# Install ffmpeg + Node.js 20 (bgutil-ytdlp-pot-provider requires Node >=20)
RUN apt-get update && apt-get install -y --no-install-recommends curl ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

WORKDIR /app

# Install Python dependencies first (for Docker layer caching)
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Verify yt-dlp can find Node.js
RUN yt-dlp --version && node --version

# Pre-download Spleeter model so first request isn't slow
RUN python -c "from spleeter.separator import Separator; Separator('spleeter:2stems')" \
    2>/dev/null || echo "Model will download on first use"

# Copy all project files
COPY . .

# Create temp directories
RUN mkdir -p /app/backend/uploads /app/backend/downloads /app/backend/separated

WORKDIR /app/backend

EXPOSE 8000

# PORT is set by Railway automatically
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
