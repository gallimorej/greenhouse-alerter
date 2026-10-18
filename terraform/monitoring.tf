# Log-based metrics over the worker's structured tick logs (src/greenhouse/worker.py), and email
# alerts on them. The sensor fail-safe is "leave the fans and alert", so these are what tells Jeff.

locals {
  worker_logs = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"greenhouse-worker\""

  log_metrics = {
    tick_errors    = "${local.worker_logs} AND severity>=ERROR"
    stale_ticks    = "${local.worker_logs} AND jsonPayload.event=\"tick\" AND jsonPayload.sensor_fresh=false"
    ok_ticks       = "${local.worker_logs} AND jsonPayload.event=\"tick\" AND jsonPayload.outcome=(\"planned\" OR \"sent\")"
    ifttt_failures = "${local.worker_logs} AND jsonPayload.event=\"tick\" AND jsonPayload.outcome=\"failed\""
  }
}

resource "google_logging_metric" "greenhouse" {
  for_each = local.log_metrics

  name   = "greenhouse/${each.key}"
  filter = each.value

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

locals {
  metric_type = { for k, m in google_logging_metric.greenhouse : k => "logging.googleapis.com/user/${m.name}" }
}

resource "google_monitoring_alert_policy" "tick_errors" {
  display_name          = "Greenhouse: worker errors"
  combiner              = "OR"
  notification_channels = [var.notification_channel]

  conditions {
    display_name = "Worker logged an error in the last 5 minutes"
    condition_threshold {
      filter          = "metric.type=\"${local.metric_type.tick_errors}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "stale_reading" {
  display_name          = "Greenhouse: no fresh sensor reading"
  combiner              = "OR"
  notification_channels = [var.notification_channel]

  documentation {
    content = "The linked sensor has had no reading newer than the stale limit (20 min by default), or no sensor is linked. Temperature control is frozen; scheduled cycles continue. Check the sensor and gateway in the admin Sensors page."
  }

  conditions {
    display_name = "Ticks without a fresh reading"
    condition_threshold {
      filter          = "metric.type=\"${local.metric_type.stale_ticks}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "no_successful_tick" {
  display_name          = "Greenhouse: no successful tick in 10 minutes"
  combiner              = "OR"
  notification_channels = [var.notification_channel]

  documentation {
    content = "The worker hasn't completed a tick in 10 minutes. The scheduler may be paused, the worker or Firestore down, or the worker failing auth. Nothing is controlling the fans."
  }

  conditions {
    display_name = "Successful ticks absent"
    condition_absent {
      filter   = "metric.type=\"${local.metric_type.ok_ticks}\" AND resource.type=\"cloud_run_revision\""
      duration = "600s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "ifttt_failures" {
  display_name          = "Greenhouse: IFTTT commands failing"
  combiner              = "OR"
  notification_channels = [var.notification_channel]

  conditions {
    display_name = "3 or more failed IFTTT commands in 10 minutes"
    condition_threshold {
      filter          = "metric.type=\"${local.metric_type.ifttt_failures}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 2
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
}
