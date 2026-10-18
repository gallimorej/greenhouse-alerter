output "worker_url" {
  value = google_cloud_run_v2_service.worker.uri
}

output "admin_url" {
  value = google_cloud_run_v2_service.admin.uri
}
