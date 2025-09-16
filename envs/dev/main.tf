    terraform {
      required_version = ">= 1.4.0"
      required_providers {
        aws = {
          source  = "hashicorp/aws"
          version = "~> 5.50"
        }
        archive = {
          source  = "hashicorp/archive"
          version = "~> 2.4"
        }
      }
    }

    provider "aws" {
      region  = var.aws_region
      profile = var.aws_profile
    }
    
    locals {
      secret_name = "${var.secret_path_prefix}/${var.saas_name}/api"
    }
    # Package lambda code
    data "archive_file" "lambda_poller_zip" {
      type        = "zip"
      source_dir  = "${path.module}/lambda/poller"
      output_path = "${path.module}/lambda_poller_zip.zip"
    }  
    data "archive_file" "lambda_preprocess_example_app_zip" {
      type        = "zip"
      source_dir  = "${path.module}/lambda/preprocess/example-app"
      output_path = "${path.module}/lambda_preprocess_example_app_zip.zip"
    }
    # IAM Role for lambda - execute role + secret manager access
    resource "aws_iam_role" "poller" {
      name = "poller-lambda-role"
      assume_role_policy = jsonencode({
        Version = "2012-10-17",
        Statement = [{
          Action = "sts:AssumeRole",
          Effect = "Allow",
          Principal = {
            Service = "lambda.amazonaws.com"
          }
        }]
      })
    }
    
    resource "aws_iam_role" "preprocess_example_app" {
      name = "preprocess-example-app-lambda-role"
      assume_role_policy = jsonencode({
        Version = "2012-10-17",
        Statement = [{
          Action = "sts:AssumeRole",
          Effect = "Allow",
          Principal = {
            Service = "lambda.amazonaws.com"
          }
        }]
      })
    }
    
    # IAM attachments/policies for preprocess example app
    resource "aws_iam_role_policy_attachment" "preprocess_example_app_basic_logs" {
      role       = aws_iam_role.preprocess_example_app.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    }

    resource "aws_iam_role_policy" "preprocess_example_app_put_metrics" {
      name = "preprocess-example-app-put-metrics"
      role = aws_iam_role.preprocess_example_app.id
      policy = jsonencode({
        Version = "2012-10-17",
        Statement = [{
          Effect = "Allow",
          Action = [
            "cloudwatch:PutMetricData"
          ],
          Resource = "*"
        }]
      })
    }


    # Set up secrets manager metadata (name only; set value via CLI to avoid TF state)
    resource "aws_secretsmanager_secret" "api_key" {
      name        = local.secret_name
      description = "API key for data poller"
      kms_key_id  = aws_kms_key.secrets_key.arn
    }

    # KMS key and alias for encrypting secrets in Secrets Manager
    resource "aws_kms_key" "secrets_key" {
      description             = "KMS key for Secrets Manager (observability)"
      deletion_window_in_days = 7
      enable_key_rotation     = true
    }

    resource "aws_kms_alias" "secrets_alias" {
      name          = "alias/observability/secrets"
      target_key_id = aws_kms_key.secrets_key.key_id
    }
    # Least-privilege read to a secrets prefix (supports multiple apps)
    data "aws_caller_identity" "current" {}
    
    resource "aws_iam_role_policy" "poller_secrets_read" {
      name = "poller-secrets-read"
      role = aws_iam_role.poller.id
      policy = jsonencode({
        Version = "2012-10-17",
        Statement = [
          {
            Effect = "Allow",
            Action = [
              "secretsmanager:GetSecretValue",
              "secretsmanager:DescribeSecret"
            ],
            Resource = "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${var.secret_path_prefix}/*"
          },
          {
            Effect = "Allow",
            Action = [
              "kms:Decrypt"
            ],
            Resource = aws_kms_key.secrets_key.arn
          }
        ]
      })
    }
    
    resource "aws_iam_role_policy_attachment" "poller_basic_logs" {
      role       = aws_iam_role.poller.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    }

    # DynamoDB table for per-app configuration
    resource "aws_dynamodb_table" "app_config" {
      name         = "ObservabilityAppConfig"
      billing_mode = "PAY_PER_REQUEST"
      hash_key     = "appName"

      attribute {
        name = "appName"
        type = "S"
      }
    }

    # Step Functions execution role (to read config and invoke poller Lambda)
    resource "aws_iam_role" "sfn_exec" {
      name = "sfn-observability-exec"
      assume_role_policy = jsonencode({
        Version = "2012-10-17",
        Statement = [{
          Effect = "Allow",
          Principal = { Service = "states.amazonaws.com" },
          Action = "sts:AssumeRole"
        }]
      })
    }

    resource "aws_iam_role_policy" "sfn_exec_policy" {
      name = "sfn-observability-permissions"
      role = aws_iam_role.sfn_exec.id
      policy = jsonencode({
        Version = "2012-10-17",
        Statement = [
          {
            Effect = "Allow",
            Action = [
              "dynamodb:GetItem"
            ],
            Resource = aws_dynamodb_table.app_config.arn
          },
          {
            Effect = "Allow",
            Action = [
              "lambda:InvokeFunction"
            ],
            Resource = [
              aws_lambda_function.poller.arn,
              "${aws_lambda_function.poller.arn}:*"
            ]
          },
          {
            Effect = "Allow",
            Action = [
              "lambda:InvokeFunction"
            ],
            Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:observability-preprocess-*"
          },
          {
            Effect = "Allow",
            Action = [
              "cloudwatch:PutMetricData"
            ],
            Resource = "*"
          }
        ]
      })
    }

    resource "aws_cloudwatch_log_group" "poller" {
      name              = "/aws/lambda/observability-poller"
      retention_in_days = 7
    }

    resource "aws_cloudwatch_log_group" "preprocess_example_app" {
      name              = "/aws/lambda/observability-preprocess-example-app"
      retention_in_days = 7
    }

    resource "aws_lambda_function" "poller" {
      function_name = "observability-poller"
      role          = aws_iam_role.poller.arn
      runtime       = "python3.12"
      handler       = "handler.lambda_handler"

      filename         = data.archive_file.lambda_poller_zip.output_path
      source_code_hash = data.archive_file.lambda_poller_zip.output_base64sha256

      timeout = 60
      environment {
        variables = {
          # Optional defaults; Step Functions event can override
          API_KEY_HEADER  = "Authorization"
          API_KEY_PREFIX  = "Bearer "
          LOG_LEVEL       = "DEBUG"           # DEBUG for verbose logs
          RETURN_DEBUG    = "true"          # true to embed debug in response
        }
      }
      depends_on = [
        aws_iam_role_policy_attachment.poller_basic_logs
      ]
    }

    resource "aws_lambda_function" "preprocess_example_app" {
      function_name = "observability-preprocess-example-app"
      role          = aws_iam_role.preprocess_example_app.arn
      runtime       = "python3.12"
      handler       = "handler.lambda_handler"

      filename         = data.archive_file.lambda_preprocess_example_app_zip.output_path
      source_code_hash = data.archive_file.lambda_preprocess_example_app_zip.output_base64sha256

      timeout = 20
      environment {
        variables = {
          METRIC_NAMESPACE = "Observability/ExampleApp"
        }
      }

      depends_on = [
        aws_iam_role_policy_attachment.preprocess_example_app_basic_logs
      ]
    }

    # Step Functions state machine: Get config from DynamoDB, then invoke poller Lambda
    resource "aws_sfn_state_machine" "observability" {
      name     = "observability-saas-monitor-sm"
      role_arn = aws_iam_role.sfn_exec.arn

      definition = jsonencode({
        Comment = "Fetch per-app config and invoke generic poller",
        StartAt = "GetConfig",
        States = {
          GetConfig = {
            Type = "Task",
            Resource = "arn:aws:states:::dynamodb:getItem",
            Parameters = {
              TableName      = aws_dynamodb_table.app_config.name,
              Key            = {
                appName = {
                  "S.$" = "$.appName"
                }
              },
              ConsistentRead = true
            },
            ResultSelector = {
              "method.$"       = "$.Item.method.S",
              "url.$"          = "$.Item.url.S",
              "headers.$"      = "$.Item.headers.M",
              "query.$"        = "$.Item.query.M",
              "timeout.$"      = "$.Item.timeout.N",
              "secret_name.$"  = "$.Item.secret_name.S",
              "json_key.$"     = "$.Item.json_key.S",
              "auth_header.$"  = "$.Item.auth_header.S",
              "auth_prefix.$"  = "$.Item.auth_prefix.S",
              "retry.$"        = "$.Item.retry.M",
              "preprocess_name.$" = "$.Item.preprocess_name.S"
            },
            ResultPath = "$.config",
            Next       = "InvokePoller"
          },
          InvokePoller = {
            Type     = "Task",
            Resource = "arn:aws:states:::lambda:invoke",
            Parameters = {
              FunctionName = aws_lambda_function.poller.arn,
              Payload = {
                request = {
                  "method.$"  = "$.config.method",
                  "url.$"     = "$.config.url",
                  "headers.$" = "$.config.headers",
                  "query.$"   = "$.config.query",
                  "timeout.$" = "$.config.timeout"
                },
                auth = {
                  "secret_name.$" = "$.config.secret_name",
                  "json_key.$"    = "$.config.json_key",
                  "header_name.$" = "$.config.auth_header",
                  "prefix.$"      = "$.config.auth_prefix"
                },
                "retry.$" = "$.config.retry"
              }
            },
            ResultPath     = "$.poll",
            Next           = "ChoiceAfterPoller"
          },
          ChoiceAfterPoller = {
            Type = "Choice",
            Choices = [
              { "Variable" = "$.poll.Payload.ok", "BooleanEquals" = true, "Next" = "InvokePreprocess" }
            ],
            Default = "ReportFailure"
          },
          InvokePreprocess = {
            Type     = "Task",
            Resource = "arn:aws:states:::lambda:invoke",
            Parameters = {
              "FunctionName.$" = "$.config.preprocess_name",
              Payload = {
                "appName.$" = "$.appName",
                "poll.$"    = "$.poll.Payload",
                "config.$"  = "$.config"
              }
            },
            End = true
          },
          ReportFailure = {
            Type     = "Task",
            Resource = "arn:aws:states:::aws-sdk:cloudwatch:putMetricData",
            Parameters = {
              Namespace = "Observability/Poller",
              MetricData = [
                {
                  MetricName = "PollFailed",
                  Unit       = "Count",
                  Value      = 1,
                  Dimensions = [
                    { Name = "AppName", "Value.$" = "$.appName" }
                  ]
                }
              ]
            },
            End = true
          }
        }
      })

      type = "STANDARD"
    }

    # CloudWatch alarm for any failed executions of the state machine
    resource "aws_cloudwatch_metric_alarm" "sfn_failed_executions" {
      alarm_name          = "observability-sfn-executions-failed"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      evaluation_periods  = 1
      metric_name         = "ExecutionsFailed"
      namespace           = "AWS/States"
      period              = 60
      statistic           = "Sum"
      threshold           = 1
      treat_missing_data  = "notBreaching"

      dimensions = {
        StateMachineArn = aws_sfn_state_machine.observability.arn
      }
    }

    # EventBridge Scheduler role to start Step Functions executions
    resource "aws_iam_role" "scheduler_exec" {
      name = "scheduler-observability-exec"
      assume_role_policy = jsonencode({
        Version = "2012-10-17",
        Statement = [{
          Effect = "Allow",
          Principal = { Service = "scheduler.amazonaws.com" },
          Action = "sts:AssumeRole"
        }]
      })
    }

    resource "aws_iam_role_policy" "scheduler_exec_policy" {
      name = "scheduler-observability-permissions"
      role = aws_iam_role.scheduler_exec.id
      policy = jsonencode({
        Version = "2012-10-17",
        Statement = [{
          Effect = "Allow",
          Action = [
            "states:StartExecution"
          ],
          Resource = aws_sfn_state_machine.observability.arn
        }]
      })
    }

    # Example schedule (runs every 5 minutes) with appName payload
    resource "aws_scheduler_schedule" "app_schedule" {
      name                         = "observability-saas-monitor-example-app-5m"
      group_name                   = "default"
      schedule_expression          = "rate(5 minutes)"
      flexible_time_window {
        mode = "OFF"
      }
      target {
        arn      = aws_sfn_state_machine.observability.arn
        role_arn = aws_iam_role.scheduler_exec.arn
        input    = jsonencode({ appName = var.saas_name })
      }
    }
