# Terraform module

A search Lambda, a nightly import Lambda, and the S3 bucket they share. Python 3.13, arm64.

```hcl
module "cards" {
  source = "git::https://github.com/<owner>/tcg-oracle-searcher-lambda.git//terraform?ref=v0.1.0"

  name_prefix     = "myapp-prod-cards"
  lambda_zip_path = "${path.module}/tcg-oracle-searcher-lambda.zip" # the zip attached to the same release tag
  tags            = { project = "myapp" }
}

# Let your app call the search Lambda.
resource "aws_iam_role_policy" "call_search" {
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = module.cards.search_function_arn }]
  })
}
```

## Inputs

| Name | Default | |
| --- | --- | --- |
| `name_prefix` | required | Prefix for every resource name. |
| `lambda_zip_path` | required | Path to the Lambda zip. |
| `bucket_name` | `null` | Existing bucket to use. `null` creates `<name_prefix>-cards` (public access blocked, SSE, builds under `cards/builds/` expire after 7 days). A bucket you pass in is not changed: add that lifecycle rule yourself. |
| `search_memory_mb` | `1024` | Search Lambda memory. |
| `import_schedule` | `cron(17 7 * * ? *)` | EventBridge schedule for the import. `null` turns it off. |
| `tags` | `{}` | Tags on every taggable resource. |

## Outputs

`search_function_name`, `search_function_arn`, `import_function_name`, `bucket_name`.

## Fixed settings

| | Search | Import |
| --- | --- | --- |
| Timeout | 10 s | 300 s |
| Memory | `search_memory_mb` | 1024 MB |
| Ephemeral storage | 512 MB | 1024 MB |
| S3 access | `GetObject` on `cards/*` | `GetObject`, `PutObject` on `cards/*` |

Log groups keep 14 days. Run the import once by hand after the first apply (see the main README), or the search Lambda has no card file to load.
