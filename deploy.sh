#!/usr/bin/env bash
# Deploy Elmeeda Voice Agent to Google Cloud Run with GPU support.
set -euo pipefail

echo "==> Deploying to Cloud Run (GPU: nvidia-l4)..."
gcloud run deploy elmeeda-voice-agent \
  --source . \
  --region us-central1 \
  --project hazel-hall-487120-v3 \
  --gpu 1 \
  --gpu-type nvidia-l4 \
  --memory 16Gi \
  --cpu 4 \
  --allow-unauthenticated \
  --port 8080 \
  --quiet

echo "==> Deployment complete."
gcloud run services describe elmeeda-voice-agent \
  --project hazel-hall-487120-v3 \
  --region us-central1 \
  --format "value(status.url)"
