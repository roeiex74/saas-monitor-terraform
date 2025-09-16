    variable "aws_region" {
      description = "The AWS region to deploy resources in"
      type        = string
      default     = "eu-central-1"
    }

    variable "aws_profile" {
      description = "AWS shared config/credentials profile name"
      type        = string
      default     = "509615516105_SSO-MyDepartment-Admin"
    }

    variable "saas_name" {
      description = "SaaS identifier (e.g., office-365, roei-test)"
      type        = string
    }

    variable "secret_path_prefix" {
      description = "Prefix for Secrets Manager path"
      type        = string
      default     = "observability/saas"
    }
