resource "aws_cloudwatch_metric_alarm" "reactive_comparator" {
  alarm_name                = "${var.name_prefix}-reactive-comparator"
  alarm_description         = "Reactive-only comparator used to measure Pulse detection lead time"
  namespace                 = var.pulse_metric_namespace
  metric_name               = "ReactiveComparator"
  dimensions                = { AutoScalingGroupName = local.asg_name }
  comparison_operator       = "GreaterThanOrEqualToThreshold"
  evaluation_periods        = 1
  datapoints_to_alarm       = 1
  period                    = 60
  statistic                 = "Maximum"
  threshold                 = var.reactive_alarm_threshold
  treat_missing_data        = "notBreaching"
  actions_enabled           = length(var.reactive_alarm_action_arns) > 0
  alarm_actions             = var.reactive_alarm_action_arns
  ok_actions                = []
  insufficient_data_actions = []
  tags                      = local.common_tags
}
