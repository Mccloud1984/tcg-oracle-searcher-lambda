variable "name_prefix" {
  type        = string
  description = "Prefix for every resource name, e.g. \"myapp-prod-cards\"."
}

variable "bucket_name" {
  type        = string
  default     = null
  description = "Existing S3 bucket to keep the card files in. When null, the module creates one named \"<name_prefix>-cards\". Files go under the cards/ prefix."
}

variable "search_memory_mb" {
  type        = number
  default     = 1024
  description = "Memory of the search Lambda (it loads the whole card file, about 180 MB, into /tmp)."
}

variable "import_schedule" {
  type        = string
  default     = "cron(17 7 * * ? *)"
  description = "EventBridge schedule expression for the import Lambda. null turns the schedule off (run the import by hand)."
}

variable "sweep_schedule" {
  type        = string
  default     = "cron(23 6 ? * SUN *)"
  description = "EventBridge schedule expression for the sweep Lambda (the is: tag sweep, about 10 minutes). null turns the schedule off (run the sweep by hand)."
}

variable "lambda_zip_path" {
  type        = string
  description = "Path to tcg-oracle-searcher-lambda.zip (scripts/build_zip.sh, or the zip attached to a GitHub release)."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags for every resource that takes them."
}
