# The bucket is created by this stack; see README.md for rebuilding from scratch.
terraform {
  backend "gcs" {
    bucket = "greenhouse-alerter-tfstate"
    prefix = "bootstrap"
  }
}
