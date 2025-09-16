    output "lambda_function_name" {
      value = aws_lambda_function.poller.function_name
    }

    output "lambda_function_arn" {
      value = aws_lambda_function.poller.arn
    }

    output "app_config_table_name" {
      value = aws_dynamodb_table.app_config.name
    }

    output "step_functions_role_arn" {
      value = aws_iam_role.sfn_exec.arn
    }

    output "state_machine_arn" {
      value = aws_sfn_state_machine.observability.arn
    }

    output "scheduler_role_arn" {
      value = aws_iam_role.scheduler_exec.arn
    }

    output "scheduler_name" {
      value = aws_scheduler_schedule.app_schedule.name
    }

    output "sfn_failed_alarm_name" {
      value = aws_cloudwatch_metric_alarm.sfn_failed_executions.alarm_name
    }

    output "preprocess_example_function_name" {
      value = aws_lambda_function.preprocess_example_app.function_name
    }

    output "preprocess_example_function_arn" {
      value = aws_lambda_function.preprocess_example_app.arn
    }
