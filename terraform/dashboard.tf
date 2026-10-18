# The Greenhouse operations dashboard in Cloud Monitoring: what the worker saw and did, on the same
# log-based metrics the alerts use, plus a temperature metric extracted from the tick logs.

resource "google_logging_metric" "temperature" {
  name   = "greenhouse/temperature_f"
  filter = "${local.worker_logs} AND jsonPayload.event=\"tick\" AND jsonPayload.sensor_fresh=true"

  value_extractor = "EXTRACT(jsonPayload.temperature_f)"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "[degF]"
  }

  bucket_options {
    linear_buckets {
      num_finite_buckets = 100
      width              = 1
      offset             = 30
    }
  }
}

locals {
  cloud_run_worker = "resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"greenhouse-worker\""

  count_chart = {
    for k in ["ok_ticks", "stale_ticks", "ifttt_failures", "tick_errors"] : k => {
      timeSeriesQuery = {
        timeSeriesFilter = {
          filter = "metric.type=\"${local.metric_type[k]}\" AND resource.type=\"cloud_run_revision\""
          aggregation = {
            alignmentPeriod    = "300s"
            perSeriesAligner   = "ALIGN_SUM"
            crossSeriesReducer = "REDUCE_SUM"
          }
        }
      }
      plotType = "LINE"
    }
  }
}

resource "google_monitoring_dashboard" "greenhouse" {
  dashboard_json = jsonencode({
    displayName = "Greenhouse"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          xPos = 0, yPos = 0, width = 12, height = 4
          widget = {
            title = "Greenhouse temperature (°F, median per 5 min)"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.temperature.name}\" AND resource.type=\"cloud_run_revision\""
                    # The aligner takes the median of each series' readings in the window; normally
                    # one revision is serving, and the mean only matters across a deploy.
                    aggregation = {
                      alignmentPeriod    = "300s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_50"
                      crossSeriesReducer = "REDUCE_MEAN"
                    }
                  }
                }
                plotType = "LINE"
              }]
              yAxis = { label = "°F", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 0, yPos = 4, width = 6, height = 4
          widget = {
            title   = "Successful ticks per 5 min (expect 5)"
            xyChart = { dataSets = [local.count_chart.ok_ticks], yAxis = { scale = "LINEAR" } }
          }
        },
        {
          xPos = 6, yPos = 4, width = 6, height = 4
          widget = {
            title   = "Ticks without a fresh reading per 5 min"
            xyChart = { dataSets = [local.count_chart.stale_ticks], yAxis = { scale = "LINEAR" } }
          }
        },
        {
          xPos = 0, yPos = 8, width = 6, height = 4
          widget = {
            title   = "IFTTT command failures per 5 min"
            xyChart = { dataSets = [local.count_chart.ifttt_failures], yAxis = { scale = "LINEAR" } }
          }
        },
        {
          xPos = 6, yPos = 8, width = 6, height = 4
          widget = {
            title   = "Worker errors per 5 min"
            xyChart = { dataSets = [local.count_chart.tick_errors], yAxis = { scale = "LINEAR" } }
          }
        },
        {
          xPos = 0, yPos = 12, width = 6, height = 4
          widget = {
            title = "Worker request latency (p95)"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"run.googleapis.com/request_latencies\" AND ${local.cloud_run_worker}"
                    # p95 of each series' requests in the window; the worst series across a deploy.
                    aggregation = {
                      alignmentPeriod    = "300s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_95"
                      crossSeriesReducer = "REDUCE_MAX"
                    }
                  }
                }
                plotType = "LINE"
              }]
              yAxis = { label = "ms", scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, yPos = 12, width = 6, height = 4
          widget = {
            title = "Alerts"
            incidentList = {
              # The dashboard API wants "alertPolicies/{id}", not the full resource name.
              policyNames = [
                for p in [
                  google_monitoring_alert_policy.tick_errors,
                  google_monitoring_alert_policy.stale_reading,
                  google_monitoring_alert_policy.no_successful_tick,
                  google_monitoring_alert_policy.ifttt_failures,
                ] : "alertPolicies/${element(split("/", p.name), 3)}"
              ]
            }
          }
        },
      ]
    }
  })
}

output "dashboard_url" {
  value = "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.greenhouse.id), 3)}?project=${var.project_id}"
}
