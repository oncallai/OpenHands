---
name: google_cloud_run
type: knowledge
version: 1.0.0
agent: CodeActAgent
triggers:
- cloud run
- google cloud run
- gcrun
---

# Google Cloud Run Quickstart

## Prerequisites

- Google Cloud SDK (gcloud) installed and initialized
- A Google Cloud project

## Install Google Cloud SDK (if needed)

```bash
# On Debian/Ubuntu
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-sdk-456.0.0-linux-x86_64.tar.gz
 tar -xzf google-cloud-sdk-456.0.0-linux-x86_64.tar.gz
 ./google-cloud-sdk/install.sh
```

## Authenticate and Set Project

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

## Enable Cloud Run API

```bash
gcloud services enable run.googleapis.com
```

## Deploy to Cloud Run

```bash
gcloud run deploy my-service \
  --image gcr.io/cloudrun/hello \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated
```

## View Service URL

```bash
gcloud run services describe my-service --platform managed --region us-central1 --format 'value(status.url)'
```
