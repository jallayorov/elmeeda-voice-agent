# Elmeeda Voice Agent — Cloud Run GPU (NVIDIA L4) container
# Uses CUDA 12.4 base for PersonaPlex / Moshi model inference.

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# System dependencies for audio processing, Python, and git (for cloning)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip python3.11-dev \
        libopus0 libopus-dev libsndfile1 ffmpeg \
        git curl ca-certificates && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (non-CUDA first for layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install PyTorch with CUDA 12.4 support
RUN pip install --no-cache-dir \
        torch torchaudio --index-url https://download.pytorch.org/whl/cu124

# Clone NVIDIA PersonaPlex repo and install moshi from source
RUN git clone https://github.com/NVIDIA/PersonaPlex.git /opt/personaplex && \
    pip install --no-cache-dir /opt/personaplex/moshi

# Copy application code
COPY app.py twilio_bridge.py elmeeda_client.py persona_config.py ./

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

CMD ["python", "-u", "app.py"]
