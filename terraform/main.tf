provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_cloud_scheduler_job" "greenhouse-temp-check" {
  name             = "greenhouse-temp-check"
  description      = "This is the job that checks the temperature in the greenhouse"
  schedule         = "*/15 6-22 * * *" # Check every 15 minutes between the hours of 6a and 10p
  time_zone        = var.job_time_zone
  app_engine_http_target {
    app_engine_routing {
      service  = var.app_engine_service
      version  = var.app_engine_version
      instance = var.app_engine_instance
    }
    relative_uri = var.temp_check_job_uri
    http_method  = "GET"
  }
}

resource "google_cloud_scheduler_job" "start-greenhouse-fan-cycle" {
  name             = "start-greenhouse-fan-cycle"
  description      = "This is the job that starts the greenhouse fan cycle"
  schedule         = "5 * * * *"
  time_zone        = var.job_time_zone
  app_engine_http_target {
    app_engine_routing {
      service  = var.app_engine_service
      version  = var.app_engine_version
      instance = var.app_engine_instance
    }
    relative_uri = var.start_fan_cycle_job_uri
    http_method  = "GET"
  }
}

resource "google_cloud_scheduler_job" "stop-greenhouse-fan-cycle" {
  name             = "stop-greenhouse-fan-cycle"
  description      = "This is the job that stops the greenhouse fan cycle"
  schedule         = "7 * * * *" # The cycle runs for two minutes
  time_zone        = var.job_time_zone
  app_engine_http_target {
    app_engine_routing {
      service  = var.app_engine_service
      version  = var.app_engine_version
      instance = var.app_engine_instance
    }
    relative_uri = var.stop_fan_cycle_job_uri
    http_method  = "GET"
  }
}