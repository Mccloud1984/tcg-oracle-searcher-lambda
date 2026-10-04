# Terraform module

A search Lambda, a nightly import Lambda, a weekly sweep Lambda, and the S3 bucket they share. Python 3.13, arm64.

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
| `sweep_schedule` | `cron(23 6 ? * SUN *)` | EventBridge schedule for the sweep. `null` turns it off. |
| `tags` | `{}` | Tags on every taggable resource. |

## Outputs

`search_function_name`, `search_function_arn`, `import_function_name`, `sweep_function_name`, `bucket_name`.

## Fixed settings

| | Search | Import | Sweep |
| --- | --- | --- | --- |
| Timeout | 10 s | 300 s | 900 s |
| Memory | `search_memory_mb` | 1024 MB | 256 MB |
| Ephemeral storage | 512 MB | 1024 MB | default |
| S3 access | `GetObject` on `cards/*` | `GetObject`, `PutObject` on `cards/*`; `GetObject` on `sweeps/*` | `GetObject`, `PutObject` on `sweeps/*` |

The sweep asks Scryfall for the `is:` tags a card row can't answer (about 213 requests at 1 a second, about 10 minutes) and stores `sweeps/is_tags.json`; the import applies the latest file. It stops starting new tags with under 60 s left and keeps the previous entries for tags it did not finish. Without a sweep file the import still works, just without those tags. Run the sweep once by hand after the first apply, before the first import.

Log groups keep 14 days. Run the import once by hand after the first apply (see the main README), or the search Lambda has no card file to load.
