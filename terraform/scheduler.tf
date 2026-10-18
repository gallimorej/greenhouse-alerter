resource "google_cloud_scheduler_job" "tick" {
  name             = "greenhouse-tick"
  description      = "Runs one controller tick on the worker every minute"
  schedule         = "* * * * *"
  time_zone        = "America/New_York"
  region           = var.region
  attempt_deadline = "30s"
  paused           = var.tick_paused

  # The next tick is the retry.
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${local.worker_url}/tasks/tick"

    oidc_token {
      service_account_email = local.scheduler_sa
      audience              = local.worker_url
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.worker_invoker]
}
