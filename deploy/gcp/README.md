# Gary on Google Cloud

One always-on `e2-micro` VM (free tier) in `us-east1-b`, running the published image
`ghcr.io/magnentius/gary:latest` on Container-Optimized OS. ARCHITECTURE.md §11 has the why.

- **No public IP.** Gary only connects out, through Cloud NAT (`gary-router`, `gary-nat`).
  Nothing on the internet can reach the VM; SSH comes in through IAP only.
- **No secrets in the repo or on the VM's metadata.** The env file (`gary-env`) is in Secret
  Manager. The VM's service account, `gary-vm`, can read it and write logs, nothing else. The
  image is public, so the VM pulls it without logging in.
- **The campaign** is a file backend at `/var/lib/gary/campaign` on the VM's disk, which is
  snapshotted every night and kept a week (`gary-nightly`).

## Everyday

| To | Run |
|---|---|
| Read Gary's log | `gcloud logging read 'logName:"gcplogs-docker-driver"' --freshness=1h --limit 50` |
| Update to the newest image | `gcloud compute instances reset gary --zone us-east1-b` (every boot pulls `:latest`) |
| Change a setting | Edit `~/.config/gary/env`, then `gcloud secrets versions add gary-env --data-file=$HOME/.config/gary/env`, then reset the VM |
| Get a shell | `gcloud compute ssh gary --zone us-east1-b --tunnel-through-iap` |
| Restore the campaign | Create a disk from a `gary-nightly` snapshot and swap it in as the boot disk |

Add `--project YOUR_PROJECT` to each (your Google Cloud project id), or set it once with
`gcloud config set project YOUR_PROJECT`.

**Run only one Gary.** Don't start a local one against the same bot token while the VM is up:
both would answer.

## Building it from nothing

1. Enable Secret Manager and Logging, and create the service account:
   ```bash
   gcloud services enable secretmanager.googleapis.com logging.googleapis.com
   gcloud iam service-accounts create gary-vm --display-name="Gary VM"
   ```
2. Store the env file as a secret:
   ```bash
   gcloud secrets create gary-env --data-file=$HOME/.config/gary/env
   ```
3. Let the VM read it and write logs:
   ```bash
   SA=gary-vm@YOUR_PROJECT.iam.gserviceaccount.com
   gcloud secrets add-iam-policy-binding gary-env --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
   gcloud projects add-iam-policy-binding YOUR_PROJECT \
     --member=serviceAccount:$SA --role=roles/logging.logWriter --condition=None
   ```
4. `deploy/gcp/create.sh`: the router, NAT, IAP firewall rule, the VM, and its snapshots.

`startup.sh` is the VM's boot script: it reads the env secret, pulls the public
image and runs it with the campaign mounted. Changing it needs
`gcloud compute instances add-metadata gary --zone us-east1-b --metadata-from-file startup-script=deploy/gcp/startup.sh`
and a reset.
