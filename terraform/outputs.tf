output "search_function_name" {
  value       = aws_lambda_function.search.function_name
  description = "Name of the search Lambda. Invoke with {\"q\": ..., \"order\": ..., \"dir\": ..., \"page\": ...}."
}

output "search_function_arn" {
  value       = aws_lambda_function.search.arn
  description = "ARN of the search Lambda, for the caller's lambda:InvokeFunction permission."
}

output "import_function_name" {
  value       = aws_lambda_function.import.function_name
  description = "Name of the import Lambda. Invoke it once by hand after the first apply."
}

output "bucket_name" {
  value       = local.bucket_name
  description = "The S3 bucket holding the card files."
}
