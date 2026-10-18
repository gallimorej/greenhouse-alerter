# Secret containers only. Values are added out of band (gcloud secrets versions add) so they never
# land in Terraform state.

locals {
  existing_secrets = ["SENSORPUSH_USER", "SENSORPUSH_PASSWORD", "IFTTT_KEY"]
  runtime_secrets  = concat(local.existing_secrets, ["FLASK_SECRET_KEY"])

  # Which runtime reads which secret; matches ROLE_SECRETS in src/greenhouse/config.py.
  secret_readers = {
    "worker/SENSORPUSH_USER"     = { secret = "SENSORPUSH_USER", sa = google_service_account.worker.email }
    "worker/SENSORPUSH_PASSWORD" = { secret = "SENSORPUSH_PASSWORD", sa = google_service_account.worker.email }
    "worker/IFTTT_KEY"           = { secret = "IFTTT_KEY", sa = google_service_account.worker.email }
    "admin/SENSORPUSH_USER"      = { secret = "SENSORPUSH_USER", sa = google_service_account.admin.email }
    "admin/SENSORPUSH_PASSWORD"  = { secret = "SENSORPUSH_PASSWORD", sa = google_service_account.admin.email }
    "admin/FLASK_SECRET_KEY"     = { secret = "FLASK_SECRET_KEY", sa = google_service_account.admin.email }
  }
}

# Adopt the secrets that already exist (automatic replication, matching this config) instead of
# recreating them.
import {
  for_each = toset(local.existing_secrets)
  to       = google_secret_manager_secret.runtime[each.value]
  id       = "projects/${var.project_id}/secrets/${each.value}"
}

resource "google_secret_manager_secret" "runtime" {
  for_each = toset(local.runtime_secrets)

  secret_id = each.value

  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_iam_member" "runtime_access" {
  for_each = local.secret_readers

  secret_id = google_secret_manager_secret.runtime[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.sa}"
}
