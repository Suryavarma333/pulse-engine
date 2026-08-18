data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  target_asg_arn = format(
    "arn:%s:autoscaling:%s:%s:autoScalingGroup:*:autoScalingGroupName/%s",
    data.aws_partition.current.partition,
    var.aws_region,
    data.aws_caller_identity.current.account_id,
    local.asg_name,
  )
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "pulse" {
  name               = "${var.name_prefix}-runtime"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_policy" "pulse_runtime" {
  name        = "${var.name_prefix}-runtime"
  description = "Least-privilege runtime reads and one bounded target-ASG capacity mutation"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid      = "SetOnlyTargetDesiredCapacity"
          Effect   = "Allow"
          Action   = ["autoscaling:SetDesiredCapacity"]
          Resource = [local.target_asg_arn]
        },
        {
          Sid      = "ReadTargetCapacity"
          Effect   = "Allow"
          Action   = ["autoscaling:DescribeAutoScalingGroups"]
          Resource = "*"
        },
        {
          Sid    = "ReadCloudWatchSignals"
          Effect = "Allow"
          Action = [
            "cloudwatch:GetMetricData",
            "cloudwatch:GetMetricStatistics",
            "cloudwatch:ListMetrics",
          ]
          Resource = "*"
        },
        {
          Sid      = "PublishPulseMetricsOnly"
          Effect   = "Allow"
          Action   = ["cloudwatch:PutMetricData"]
          Resource = "*"
          Condition = {
            StringEquals = {
              "cloudwatch:namespace" = var.pulse_metric_namespace
            }
          }
        },
      ],
      var.sqs_queue_arn == null ? [] : [
        {
          Sid      = "ReadConfiguredQueueDepth"
          Effect   = "Allow"
          Action   = ["sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
          Resource = [var.sqs_queue_arn]
        },
      ],
    )
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "pulse_runtime" {
  role       = aws_iam_role.pulse.name
  policy_arn = aws_iam_policy.pulse_runtime.arn
}

resource "aws_iam_instance_profile" "pulse" {
  name = "${var.name_prefix}-runtime"
  role = aws_iam_role.pulse.name
  tags = local.common_tags
}
