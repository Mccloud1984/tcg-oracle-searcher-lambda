# tcg-oracle-searcher-lambda: a search Lambda, a nightly import Lambda and a weekly sweep Lambda over one S3 bucket.
# Card files: cards/builds/<build>.sqlite.gz, and cards/latest.json pointing at the one to serve.
# The sweep Lambda keeps the is: tag sweep in sweeps/is_tags.json; the import applies it.

locals {
  bucket_name = var.bucket_name == null ? aws_s3_bucket.cards[0].id : var.bucket_name
  bucket_arn  = "arn:aws:s3:::${local.bucket_name}"
  cards_arn   = "${local.bucket_arn}/cards/*"
  sweeps_arn  = "${local.bucket_arn}/sweeps/*"
  functions   = toset(["search", "import", "sweep"])
  runtime     = "python3.13"
  arch        = ["arm64"]
  zip_hash    = filebase64sha256(var.lambda_zip_path)
}

# --- bucket -------------------------------------------------------------------

resource "aws_s3_bucket" "cards" {
  count  = var.bucket_name == null ? 1 : 0
  bucket = "${var.name_prefix}-cards"
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "cards" {
  count                   = var.bucket_name == null ? 1 : 0
  bucket                  = aws_s3_bucket.cards[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cards" {
  count  = var.bucket_name == null ? 1 : 0
  bucket = aws_s3_bucket.cards[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cards" {
  count  = var.bucket_name == null ? 1 : 0
  bucket = aws_s3_bucket.cards[0].id

  rule {
    id     = "expire-old-builds"
    status = "Enabled"

    filter {
      prefix = "cards/builds/"
    }

    expiration {
      days = 7
    }
  }
}

# --- roles --------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "logs" {
  for_each = local.functions

  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fn[each.key].arn}:*"]
  }
}

data "aws_iam_policy_document" "search_s3" {
  statement {
    actions   = ["s3:GetObject"]
    resources = [local.cards_arn]
  }
}

# The import reads the sweep file the sweep Lambda writes.
data "aws_iam_policy_document" "import_s3" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = [local.cards_arn]
  }

  statement {
    actions   = ["s3:GetObject"]
    resources = [local.sweeps_arn]
  }
}

data "aws_iam_policy_document" "sweep_s3" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = [local.sweeps_arn]
  }
}

resource "aws_iam_role" "fn" {
  for_each           = local.functions
  name               = "${var.name_prefix}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "logs" {
  for_each = aws_iam_role.fn
  name     = "logs"
  role     = each.value.id
  policy   = data.aws_iam_policy_document.logs[each.key].json
}

resource "aws_iam_role_policy" "search_s3" {
  name   = "cards-read"
  role   = aws_iam_role.fn["search"].id
  policy = data.aws_iam_policy_document.search_s3.json
}

resource "aws_iam_role_policy" "import_s3" {
  name   = "cards-write"
  role   = aws_iam_role.fn["import"].id
  policy = data.aws_iam_policy_document.import_s3.json
}

resource "aws_iam_role_policy" "sweep_s3" {
  name   = "sweeps-readwrite"
  role   = aws_iam_role.fn["sweep"].id
  policy = data.aws_iam_policy_document.sweep_s3.json
}

# --- logs ---------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${var.name_prefix}-${each.key}"
  retention_in_days = 14
  tags              = var.tags
}

# --- functions ----------------------------------------------------------------

resource "aws_lambda_function" "search" {
  function_name    = "${var.name_prefix}-search"
  role             = aws_iam_role.fn["search"].arn
  runtime          = local.runtime
  architectures    = local.arch
  handler          = "oracle_searcher.handlers.search_handler.handler"
  filename         = var.lambda_zip_path
  source_code_hash = local.zip_hash
  memory_size      = var.search_memory_mb
  timeout          = 10

  ephemeral_storage {
    size = 512
  }

  environment {
    variables = { CARDS_BUCKET = local.bucket_name }
  }

  tags       = var.tags
  depends_on = [aws_cloudwatch_log_group.fn]
}

resource "aws_lambda_function" "import" {
  function_name    = "${var.name_prefix}-import"
  role             = aws_iam_role.fn["import"].arn
  runtime          = local.runtime
  architectures    = local.arch
  handler          = "oracle_searcher.handlers.import_handler.handler"
  filename         = var.lambda_zip_path
  source_code_hash = local.zip_hash
  memory_size      = 1024
  timeout          = 300

  ephemeral_storage {
    size = 1024
  }

  environment {
    variables = { CARDS_BUCKET = local.bucket_name }
  }

  tags       = var.tags
  depends_on = [aws_cloudwatch_log_group.fn]
}

resource "aws_lambda_function" "sweep" {
  function_name    = "${var.name_prefix}-sweep"
  role             = aws_iam_role.fn["sweep"].arn
  runtime          = local.runtime
  architectures    = local.arch
  handler          = "oracle_searcher.handlers.sweep_handler.handler"
  filename         = var.lambda_zip_path
  source_code_hash = local.zip_hash
  memory_size      = 256
  timeout          = 900

  environment {
    variables = { CARDS_BUCKET = local.bucket_name }
  }

  tags       = var.tags
  depends_on = [aws_cloudwatch_log_group.fn]
}

# --- nightly import -----------------------------------------------------------

resource "aws_cloudwatch_event_rule" "import" {
  count               = var.import_schedule == null ? 0 : 1
  name                = "${var.name_prefix}-import"
  schedule_expression = var.import_schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "import" {
  count = var.import_schedule == null ? 0 : 1
  rule  = aws_cloudwatch_event_rule.import[0].name
  arn   = aws_lambda_function.import.arn
}

resource "aws_lambda_permission" "import_schedule" {
  count         = var.import_schedule == null ? 0 : 1
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.import.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.import[0].arn
}

# --- weekly sweep -------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "sweep" {
  count               = var.sweep_schedule == null ? 0 : 1
  name                = "${var.name_prefix}-sweep"
  schedule_expression = var.sweep_schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "sweep" {
  count = var.sweep_schedule == null ? 0 : 1
  rule  = aws_cloudwatch_event_rule.sweep[0].name
  arn   = aws_lambda_function.sweep.arn
}

resource "aws_lambda_permission" "sweep_schedule" {
  count         = var.sweep_schedule == null ? 0 : 1
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sweep.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweep[0].arn
}
