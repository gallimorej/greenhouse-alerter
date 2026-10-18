variable "project_id" {
  description = "The GCP project ID"
  type        = string
  default     = "greenhouse-alerter"
}

variable "project_number" {
  description = "The GCP project number (used for deterministic service URLs and the IAP service agent)"
  type        = string
  default     = "267214260543"
}

variable "region" {
  description = "The GCP region"
  type        = string
  default     = "us-east1"
}

variable "image" {
  description = "Container image by digest, e.g. us-east1-docker.pkg.dev/PROJECT/greenhouse/app@sha256:..."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "Deploy images by digest, not by tag."
  }
}

variable "admin_emails" {
  description = "Comma-separated Google accounts allowed into the admin service (checked in-app, on top of IAP)"
  type        = string
}

variable "notification_channel" {
  description = "Monitoring notification channel ID for alerts (terraform/bootstrap output admin_notification_channel)"
  type        = string
}

variable "tick_paused" {
  description = "Pause the greenhouse-tick Scheduler job (rollback step a)"
  type        = bool
  default     = false
}

variable "firestore_database" {
  description = "Firestore database created by terraform/bootstrap"
  type        = string
  default     = "greenhouse"
}
