resource "google_service_account" "runtime" {
  account_id   = "greenhouse-runtime"
  display_name = "Greenhouse worker and admin runtime"

  depends_on = [google_project_service.enabled]
}

resource "google_service_account" "scheduler" {
  account_id   = "greenhouse-scheduler"
  display_name = "Greenhouse Cloud Scheduler invoker"

  depends_on = [google_project_service.enabled]
}

resource "google_service_account" "deploy" {
  account_id   = "greenhouse-deploy"
  display_name = "Greenhouse GitHub Actions deployer"

  depends_on = [google_project_service.enabled]
}

# Runtime: read/write only the greenhouse Firestore database.
resource "google_project_iam_member" "runtime_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"

  condition {
    title      = "greenhouse-database-only"
    expression = "resource.name == \"projects/${var.project_id}/databases/${google_firestore_database.greenhouse.name}\""
  }
}

# Deploy: permanent least-privilege grants for the main configuration.
locals {
  deploy_project_roles = [
    "roles/run.admin",                    # deploy services, set invoker IAM, enable IAP on the admin service
    "roles/cloudscheduler.admin",         # the single greenhouse-tick job
    "roles/monitoring.alertPolicyEditor", # Phase 2 alerts
    "roles/logging.configWriter",         # log-based metrics the alerts are built on
  ]
}

resource "google_project_iam_member" "deploy" {
  for_each = toset(local.deploy_project_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_service_account_iam_member" "deploy_acts_as" {
  for_each = {
    runtime   = google_service_account.runtime.name
    scheduler = google_service_account.scheduler.name
  }

  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_artifact_registry_repository_iam_member" "deploy_push" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_storage_bucket_iam_member" "deploy_state" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deploy.email}"
}

# Admin access through IAP. Only the admin service is IAP-protected, so a project-level grant is
# scoped in practice; granting it needs iap.admin, which only this Jeff-run bootstrap holds.
resource "google_project_iam_member" "admin_iap_access" {
  project = var.project_id
  role    = "roles/iap.httpsResourceAccessor"
  member  = "user:${var.admin_email}"
}

# Keyless GitHub Actions deploys, restricted to pushes to main of this exact repository.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "greenhouse-alerter"
  display_name                       = "greenhouse-alerter main"

  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository"    = "assertion.repository"
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.ref"           = "assertion.ref"
  }

  attribute_condition = "assertion.repository_id == \"${var.github_repository_id}\" && assertion.ref == \"refs/heads/main\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "deploy_wif" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}
