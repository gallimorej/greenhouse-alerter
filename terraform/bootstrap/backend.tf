# The bucket is created by this stack; see "How the first apply was done" in README.md.
terraform {
  backend "gcs" {
    bucket = "greenhouse-alerter-tfstate"
    prefix = "bootstrap"
  }
}
