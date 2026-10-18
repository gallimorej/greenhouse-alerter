# Bootstrap

Privileged, one-time project setup, applied from a project owner's machine rather than CI. It creates
what CI needs before CI can run: the remote state bucket, the image repository, keyless GitHub
deploys (Workload Identity Federation), the service accounts and their grants, the secret
containers, the Firestore database, admin IAP access, the alert email channel and a budget.

## Applying changes

The bootstrap was first applied on 2026-10-18, and its state is in the
`greenhouse-alerter-tfstate` bucket (prefix `bootstrap`). From a fresh checkout:

```sh
cd terraform/bootstrap
cp terraform.tfvars.example terraform.tfvars   # fill in admin_email and billing_account_id
terraform init
terraform plan
terraform apply
```

## How the first apply was done

For the record, not a general recipe: this stack is written for the `greenhouse-alerter` project,
whose three existing secrets it imports, and `backend.tf` names a state bucket the stack itself
creates. The first apply set the backend aside until the bucket existed:

```sh
mv backend.tf backend.tf.next
terraform init
terraform plan    # expect the 3 existing secrets to be imported with no changes
terraform apply
mv backend.tf.next backend.tf
terraform init -migrate-state
terraform plan    # expect "No changes": the state moved intact
```

## Secret values

Values are never in Terraform. Add the Flask signing key once:

```sh
openssl rand -base64 48 | tr -d '\n' | \
  gcloud secrets versions add FLASK_SECRET_KEY --data-file=- --project greenhouse-alerter
```

## IAP service agent

The main configuration grants the IAP service agent invoker on the admin service, so the agent has to
exist first:

```sh
gcloud beta services identity create --service=iap.googleapis.com --project greenhouse-alerter
```

## Manual Console step (no API exists for it)

The project has no organization, so IAP needs a custom OAuth client before it can admit an outside
account (done 2026-10-18):

1. Google Auth Platform → Branding → Get started: app name, support email, Audience **External**.
2. Audience → Test users: add each admin account.
3. Security → Identity-Aware Proxy → **All web services** row → ⋮ → Settings → **Custom OAuth** →
   **Auto Generate Credentials** → Save. Cloud Run services inherit it from this level; check with
   `gcloud iap settings get --project greenhouse-alerter --resource-type=iap_web`.

## GitHub

Set these repository variables (not secrets; they aren't sensitive). The deploy job in
`.github/workflows/ci.yml` runs on every push to `main` once `GCP_WORKLOAD_IDENTITY_PROVIDER` is set
(set 2026-10-18), after the test jobs pass. The `production` environment doesn't exist until the first deploy creates it, with no approval rule, so
a merge to `main` deploys without further approval.

| Variable | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `terraform output -raw workload_identity_provider` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `terraform output -raw deploy_service_account` |
| `GCP_NOTIFICATION_CHANNEL` | `terraform output -raw admin_notification_channel` |
| `ADMIN_EMAILS` | comma-separated admin Google accounts |

```sh
for v in workload_identity_provider:GCP_WORKLOAD_IDENTITY_PROVIDER \
         deploy_service_account:GCP_DEPLOY_SERVICE_ACCOUNT \
         admin_notification_channel:GCP_NOTIFICATION_CHANNEL; do
  gh variable set "${v#*:}" --body "$(terraform output -raw "${v%%:*}")"
done
gh variable set ADMIN_EMAILS --body "you@example.com"
```
