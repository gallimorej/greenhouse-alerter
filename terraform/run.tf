locals {
  runtime_sa   = "greenhouse-runtime@${var.project_id}.iam.gserviceaccount.com"
  scheduler_sa = "greenhouse-scheduler@${var.project_id}.iam.gserviceaccount.com"
  iap_agent    = "service-${var.project_number}@gcp-sa-iap.iam.gserviceaccount.com"

  # Cloud Run's deterministic URL format, so the worker's own audience needs no dependency cycle.
  worker_url = "https://greenhouse-worker-${var.project_number}.${var.region}.run.app"

  # Audience of IAP's signed header for a Cloud Run service with IAP enabled directly on it.
  admin_iap_audience = "/projects/${var.project_number}/locations/${var.region}/services/greenhouse-admin"

  common_env = {
    PROJECT_ID         = var.project_id
    FIRESTORE_DATABASE = var.firestore_database
  }
}

resource "google_cloud_run_v2_service" "worker" {
  name     = "greenhouse-worker"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = local.runtime_sa
    timeout                          = "30s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.image

      dynamic "env" {
        for_each = merge(local.common_env, {
          ROLE                      = "worker"
          GUNICORN_THREADS          = "1"
          WORKER_AUDIENCE           = local.worker_url
          SCHEDULER_SERVICE_ACCOUNT = local.scheduler_sa
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }
    }
  }
}

# Only the scheduler may invoke the worker.
resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  name     = google_cloud_run_v2_service.worker.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.scheduler_sa}"
}

resource "google_cloud_run_v2_service" "admin" {
  name        = "greenhouse-admin"
  location    = var.region
  ingress     = "INGRESS_TRAFFIC_ALL"
  iap_enabled = true

  template {
    service_account                  = local.runtime_sa
    timeout                          = "30s"
    max_instance_request_concurrency = 10

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.image

      dynamic "env" {
        for_each = merge(local.common_env, {
          ROLE             = "admin"
          GUNICORN_THREADS = "4"
          IAP_AUDIENCE     = local.admin_iap_audience
          ADMIN_EMAILS     = var.admin_emails
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }
    }
  }
}

# Only IAP may invoke the admin service; users are authorized by IAP (bootstrap grants access).
resource "google_cloud_run_v2_service_iam_member" "admin_invoker" {
  name     = google_cloud_run_v2_service.admin.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.iap_agent}"
}
