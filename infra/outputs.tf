output "autoscaling_group_name" {
  description = "Set this as PULSE_ASG_NAME only after explicitly enabling live mode."
  value       = aws_autoscaling_group.pulse.name
}

output "autoscaling_group_arn" {
  description = "ARN of the only ASG the runtime policy may mutate."
  value       = aws_autoscaling_group.pulse.arn
}

output "launch_template_id" {
  description = "IMDSv2-enforced EC2 launch template ID."
  value       = aws_launch_template.pulse.id
}

output "reactive_comparator_alarm_name" {
  description = "CloudWatch alarm used as the reactive comparator."
  value       = aws_cloudwatch_metric_alarm.reactive_comparator.alarm_name
}

output "runtime_role_arn" {
  description = "Pulse EC2 runtime role ARN."
  value       = aws_iam_role.pulse.arn
}

output "runtime_policy_arn" {
  description = "Least-privilege Pulse runtime policy ARN."
  value       = aws_iam_policy.pulse_runtime.arn
}

output "public_ip_assignment_enabled" {
  description = "Whether instances receive public IPs; no stable public endpoint is created."
  value       = var.associate_public_ip_address
}
