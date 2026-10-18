# Bootstrap

Privileged, one-time project setup, applied from a project owner's machine rather than CI. It creates
what CI needs before CI can run: the remote state bucket, the image repository, keyless GitHub
deploys (Workload Identity Federation), the service accounts and their grants, the secret
containers, the Firestore database, admin IAP access, the alert email channel and a budget.

## First apply

```sh
cd terraform/bootstrap
cp terraform.tfvars.example terraform.tfvars   # fill in admin_email and billing_account_id
terraform init
terraform plan    # expect the 3 existing secrets to be imported with no changes
terraform apply
```

Then move the state into the bucket it just created:

```sh
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

The project has no organization, so IAP needs an OAuth client before it can admit an outside account:
Console → Security → Identity-Aware Proxy → configure OAuth → **Auto generate credentials**.

## GitHub

Set these repository variables (not secrets; they aren't sensitive). The old key-based deploy
workflow is removed in this change, so nothing deploys yet. The Cloud Run deploy job (added with
the Cloud Run services) runs only on pushes to `main` and stays skipped until
`GCP_WORKLOAD_IDENTITY_PROVIDER` is set.

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
