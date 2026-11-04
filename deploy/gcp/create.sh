#!/bin/bash
# Create Gary's VM (ARCHITECTURE.md §11). Run once; deploy/gcp/README.md has the steps
# before it (the two secrets and the service account) and how to update and back up.
set -euo pipefail

PROJECT=${PROJECT:?set PROJECT to your Google Cloud project id, e.g. PROJECT=my-project ./create.sh}
ZONE=${ZONE:-us-east1-b}             # e2-micro is free tier in us-east1, us-central1, us-west1
SA=gary-vm@$PROJECT.iam.gserviceaccount.com
HERE=$(cd "$(dirname "$0")" && pwd)
REGION=${ZONE%-*}

# No public IP: Gary only connects out (Discord, Gemini, ghcr.io, Google APIs), so Cloud NAT
# gives him that and nothing on the internet can reach the VM. SSH comes in through IAP:
#   gcloud compute ssh gary --zone us-east1-b --tunnel-through-iap
gcloud services enable iap.googleapis.com --project "$PROJECT"
gcloud compute routers create gary-router --project "$PROJECT" --region "$REGION" --network default
gcloud compute routers nats create gary-nat --project "$PROJECT" --region "$REGION" \
  --router gary-router --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges
gcloud compute firewall-rules create gary-allow-iap-ssh --project "$PROJECT" --network default \
  --direction INGRESS --action allow --rules tcp:22 \
  --source-ranges 35.235.240.0/20 --target-tags gary

gcloud compute instances create gary \
  --project "$PROJECT" --zone "$ZONE" \
  --machine-type e2-micro --no-address --tags gary \
  --image-family cos-stable --image-project cos-cloud \
  --boot-disk-size 10GB --boot-disk-type pd-standard \
  --service-account "$SA" --scopes cloud-platform \
  --metadata-from-file startup-script="$HERE/startup.sh" \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring

# A snapshot of the disk every night, kept a week: the campaign is a file backend on it.
gcloud compute resource-policies create snapshot-schedule gary-nightly \
  --project "$PROJECT" --region "$REGION" \
  --daily-schedule --start-time 08:00 --max-retention-days 7 \
  --on-source-disk-delete keep-auto-snapshots
gcloud compute disks add-resource-policies gary \
  --project "$PROJECT" --zone "$ZONE" --resource-policies gary-nightly
