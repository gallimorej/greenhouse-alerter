variable "project_id" {
  description = "The GCP project ID"
  type        = string
  default     = "greenhouse-alerter"
}

# A variable rather than a google_project data source: the lookup needs the Cloud Resource Manager
# API, which this configuration is what enables.
variable "project_number" {
  description = "The GCP project number"
  type        = string
  default     = "267214260543"
}

variable "region" {
  description = "The GCP region for all regional resources"
  type        = string
  default     = "us-east1"
}

variable "github_repository" {
  description = "GitHub repository (owner/name) allowed to deploy"
  type        = string
  default     = "gallimorej/greenhouse-alerter"
}

variable "github_repository_id" {
  description = "Numeric GitHub repository ID; pinning to it means a renamed or re-created repo can't inherit trust"
  type        = string
  default     = "686784565"
}

variable "admin_email" {
  description = "Google account allowed through IAP to the admin service, and the alert email recipient"
  type        = string
}

variable "billing_account_id" {
  description = "Billing account ID for the budget alert (e.g. 000000-000000-000000)"
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly budget that triggers alert emails at 50/90/100%"
  type        = number
  default     = 10
}
