variable "aws_region" {
  description = "AWS region for the bounded demonstration stack."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a valid AWS region name."
  }
}

variable "name_prefix" {
  description = "Short lowercase prefix used for deterministic resource names."
  type        = string
  default     = "pulse-demo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,31}$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase alphanumeric or hyphen characters."
  }
}

variable "ami_id" {
  description = "Explicit AMI for the selected region and instance architecture."
  type        = string

  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.ami_id))
    error_message = "ami_id must be an EC2 AMI identifier."
  }
}

variable "instance_type" {
  description = "Low-cost EC2 instance type for the demonstration ASG."
  type        = string
  default     = "t3.micro"

  validation {
    condition     = contains(["t3.micro", "t3.small", "t4g.micro", "t4g.small"], var.instance_type)
    error_message = "Use one of the deliberately bounded low-cost instance types."
  }
}

variable "vpc_id" {
  description = "Existing VPC ID. Pulse does not create networking in the minimal stack."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must be an existing VPC identifier."
  }
}

variable "subnet_ids" {
  description = "One or more existing subnet IDs used by the ASG."
  type        = list(string)

  validation {
    condition = (
      length(var.subnet_ids) >= 1 &&
      alltrue([for value in var.subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", value))])
    )
    error_message = "subnet_ids must contain at least one existing subnet identifier."
  }
}

variable "security_group_ids" {
  description = "Existing security groups that explicitly bound instance ingress and egress."
  type        = list(string)

  validation {
    condition = (
      length(var.security_group_ids) >= 1 &&
      alltrue([for value in var.security_group_ids : can(regex("^sg-[0-9a-f]{8,17}$", value))])
    )
    error_message = "security_group_ids must contain at least one existing security group."
  }
}

variable "associate_public_ip_address" {
  description = "Optional lowest-cost direct endpoint behavior; leave false for private subnets."
  type        = bool
  default     = false
}

variable "minimum_capacity" {
  description = "ASG minimum. The demonstration starts with one instance."
  type        = number
  default     = 1

  validation {
    condition     = var.minimum_capacity == 1
    error_message = "The minimal demonstration minimum_capacity must remain exactly 1."
  }
}

variable "desired_capacity" {
  description = "Initial ASG desired capacity."
  type        = number
  default     = 1

  validation {
    condition     = var.desired_capacity >= 1 && var.desired_capacity <= 3
    error_message = "desired_capacity must be between 1 and 3."
  }
}

variable "maximum_capacity" {
  description = "Hard Terraform ASG ceiling for the small demonstration."
  type        = number
  default     = 3

  validation {
    condition     = var.maximum_capacity >= 1 && var.maximum_capacity <= 5
    error_message = "maximum_capacity must be between 1 and the conservative ceiling of 5."
  }
}

variable "default_cooldown_seconds" {
  description = "ASG cooldown supporting gradual, non-flapping recovery."
  type        = number
  default     = 120

  validation {
    condition     = var.default_cooldown_seconds >= 60 && var.default_cooldown_seconds <= 3600
    error_message = "default_cooldown_seconds must be between 60 and 3600."
  }
}

variable "root_volume_size_gib" {
  description = "Encrypted gp3 root volume size."
  type        = number
  default     = 8

  validation {
    condition     = var.root_volume_size_gib >= 8 && var.root_volume_size_gib <= 30
    error_message = "root_volume_size_gib must be between 8 and 30 GiB."
  }
}

variable "user_data" {
  description = "Optional reviewed bootstrap script. It must not contain credentials."
  type        = string
  default     = <<-EOT
    #!/bin/sh
    set -eu
    echo "Pulse demo instance initialized" | logger -t pulse
  EOT

  validation {
    condition     = length(var.user_data) <= 16384
    error_message = "user_data must stay below 16 KiB and be reviewed before apply."
  }
}

variable "pulse_metric_namespace" {
  description = "CloudWatch namespace used by the reactive comparator."
  type        = string
  default     = "Pulse/Traffic"

  validation {
    condition     = var.pulse_metric_namespace == "Pulse/Traffic"
    error_message = "The demo policy restricts metric publication to Pulse/Traffic."
  }
}

variable "reactive_alarm_threshold" {
  description = "Threshold for the 0/1 reactive comparator metric."
  type        = number
  default     = 1

  validation {
    condition     = var.reactive_alarm_threshold == 1
    error_message = "The comparator alarm threshold must remain 1."
  }
}

variable "reactive_alarm_action_arns" {
  description = "Optional pre-existing action ARNs. Empty avoids extra cost and mutation by default."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.reactive_alarm_action_arns) <= 2
    error_message = "At most two reviewed alarm actions may be supplied."
  }
}

variable "sqs_queue_arn" {
  description = "Optional existing SQS queue ARN for resource-scoped read-only depth signals."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.sqs_queue_arn == null ||
      can(regex("^arn:[^:]+:sqs:[^:]+:[0-9]{12}:[A-Za-z0-9_-]+$", var.sqs_queue_arn))
    )
    error_message = "sqs_queue_arn must be null or an existing SQS queue ARN."
  }
}

variable "tags" {
  description = "Additional non-sensitive tags."
  type        = map(string)
  default     = {}
}
