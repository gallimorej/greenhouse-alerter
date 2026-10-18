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
```

## Secret values

Values are never in Terraform. Add the Flask signing key once:

```sh
openssl rand -base64 48 | tr -d '\n' | \
  gcloud secrets versions add FLASK_SECRET_KEY --data-file=- --project greenhouse-alerter
```

## Manual Console step (no API exists for it)

The project has no organization, so IAP needs an OAuth client before it can admit an outside account:
Console → Security → Identity-Aware Proxy → configure OAuth → **Auto generate credentials**.

## GitHub

Set these repository variables (not secrets; they aren't sensitive) from `terraform output`:
`GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_DEPLOY_SERVICE_ACCOUNT`.
