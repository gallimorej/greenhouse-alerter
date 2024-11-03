variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region"
  type        = string
}

variable "service_account_email" {
  description = "The service account email"
  type        = string
}

variable "app_engine_service" {
  description = "The App Engine service to target"
  type        = string
  default     = "default"
}

variable "app_engine_version" {
  description = "The App Engine version to target"
  type        = string
  default     = ""
}

variable "app_engine_instance" {
  description = "The App Engine instance to target"
  type        = string
  default     = ""
}

variable "job_schedule" {
  description = "The schedule for the Cloud Scheduler job"
  type        = string
  default     = "5 * * * *"
}

variable "job_time_zone" {
  description = "The time zone for the Cloud Scheduler job"
  type        = string
  default     = "America/New_York"
}

variable "temp_check_job_uri" {
  description = "The URI for the temp check job"
  type        = string
}

variable "start_fan_cycle_job_uri" {
  description = "The URI for the start fan cycle job"
  type        = string
}

variable "stop_fan_cycle_job_uri" {
  description = "The URI for the stop fan cycle job"
  type        = string
}