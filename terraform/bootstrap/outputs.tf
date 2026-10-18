output "state_bucket" {
  value = google_storage_bucket.tfstate.name
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "firestore_database" {
  value = google_firestore_database.greenhouse.name
}

output "worker_service_account" {
  value = google_service_account.worker.email
}

output "admin_service_account" {
  value = google_service_account.admin.email
}

output "scheduler_service_account" {
  value = google_service_account.scheduler.email
}

output "deploy_service_account" {
  value = google_service_account.deploy.email
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "admin_notification_channel" {
  value = google_monitoring_notification_channel.admin_email.id
}
