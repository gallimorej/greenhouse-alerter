# Secret containers only. Values are added out of band (gcloud secrets versions add) so they never
# land in Terraform state.

locals {
  existing_secrets = ["SENSORPUSH_USER", "SENSORPUSH_PASSWORD", "IFTTT_KEY"]
  runtime_secrets  = concat(local.existing_secrets, ["FLASK_SECRET_KEY"])
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
  for_each = google_secret_manager_secret.runtime

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}
