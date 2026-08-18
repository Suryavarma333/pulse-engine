locals {
  asg_name             = "${var.name_prefix}-asg"
  launch_template_name = "${var.name_prefix}-launch-template"
  common_tags = merge(
    {
      Name        = var.name_prefix
      Environment = "demo"
    },
    var.tags,
  )
}

data "aws_subnet" "selected" {
  for_each = toset(var.subnet_ids)
  id       = each.value
}

resource "aws_launch_template" "pulse" {
  name                   = local.launch_template_name
  description            = "Bounded low-cost Pulse protected demo application"
  image_id               = var.ami_id
  instance_type          = var.instance_type
  update_default_version = true
  user_data              = base64encode(var.user_data)

  iam_instance_profile {
    arn = aws_iam_instance_profile.pulse.arn
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  monitoring {
    enabled = false
  }

  network_interfaces {
    associate_public_ip_address = var.associate_public_ip_address
    delete_on_termination       = true
    device_index                = 0
    security_groups             = var.security_group_ids
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      delete_on_termination = true
      encrypted             = true
      volume_size           = var.root_volume_size_gib
      volume_type           = "gp3"
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = local.common_tags
  }

  tag_specifications {
    resource_type = "volume"
    tags          = local.common_tags
  }

  tags = local.common_tags
}

resource "aws_autoscaling_group" "pulse" {
  name                      = local.asg_name
  min_size                  = var.minimum_capacity
  desired_capacity          = var.desired_capacity
  max_size                  = var.maximum_capacity
  default_cooldown          = var.default_cooldown_seconds
  health_check_type         = "EC2"
  health_check_grace_period = 120
  vpc_zone_identifier       = var.subnet_ids
  termination_policies      = ["OldestLaunchTemplate"]

  launch_template {
    id      = aws_launch_template.pulse.id
    version = "$Latest"
  }

  dynamic "tag" {
    for_each = local.common_tags
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    precondition {
      condition = alltrue([
        for subnet in data.aws_subnet.selected : subnet.vpc_id == var.vpc_id
      ])
      error_message = "Every supplied subnet must belong to vpc_id."
    }

    precondition {
      condition = (
        var.desired_capacity >= var.minimum_capacity &&
        var.desired_capacity <= var.maximum_capacity
      )
      error_message = "desired_capacity must be within the configured ASG bounds."
    }

    precondition {
      condition     = var.maximum_capacity <= 5
      error_message = "maximum_capacity cannot exceed the cost-safety ceiling of 5."
    }
  }
}
