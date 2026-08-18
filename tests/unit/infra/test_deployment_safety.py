from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
INFRA = ROOT / "infra"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_compose_dependency_order_health_and_bounded_defaults() -> None:
    compose = read("docker-compose.yml")

    assert "condition: service_healthy" in compose
    assert "condition: service_completed_successfully" in compose
    assert compose.index("  postgres:") < compose.index("  migrate:")
    assert compose.index("  migrate:") < compose.index("  demo-app:")
    assert compose.index("  demo-app:") < compose.index("  agent:")
    assert compose.index("  agent:") < compose.index("  dashboard:")
    assert "profiles: [\"load\"]" in compose
    assert "PULSE_EXECUTION_MODE: ${PULSE_EXECUTION_MODE:-dry_run}" in compose
    assert "PULSE_MAX_INSTANCE_CEILING: ${PULSE_MAX_INSTANCE_CEILING:-3}" in compose
    assert "PULSE_SIMULATED_DESIRED_CAPACITY" in compose
    assert (
        "PULSE_REACTIVE_LOAD_THRESHOLD_RPS: "
        "${PULSE_REACTIVE_LOAD_THRESHOLD_RPS:-100000}" in compose
    )
    assert "AWS_ACCESS_KEY_ID" not in compose
    assert "AWS_SECRET_ACCESS_KEY" not in compose
    assert "max-size: ${PULSE_LOG_MAX_SIZE:-10m}" in compose
    assert "max-file: ${PULSE_LOG_MAX_FILES:-3}" in compose
    assert "pulse-postgres-data:/var/lib/postgresql/data" in compose
    assert compose.count("healthcheck:") >= 4


def test_runtime_images_are_non_root_and_load_driver_is_on_demand() -> None:
    agent_image = read("agent/Dockerfile")
    dashboard_image = read("dashboard/Dockerfile")

    assert "AS agent" in agent_image
    assert "AS load" in agent_image
    assert agent_image.count("USER pulse") == 2
    assert "HEALTHCHECK" in agent_image
    assert "ENTRYPOINT [\"python\", \"load_tests/scripts/run_scenario.py\"]" in agent_image
    assert "pnpm install --frozen-lockfile" in dashboard_image
    assert "output: \"standalone\"" in read("dashboard/next.config.ts")
    assert "/app/node_modules ./node_modules" in dashboard_image
    assert "USER pulse" in dashboard_image
    assert "HEALTHCHECK" in dashboard_image


def test_terraform_defaults_enforce_imdsv2_and_cost_bounds() -> None:
    variables = read("infra/variables.tf")
    main = read("infra/main.tf")

    assert re.search(r'variable "minimum_capacity"[\s\S]+?default\s+=\s+1', variables)
    assert re.search(r'variable "desired_capacity"[\s\S]+?default\s+=\s+1', variables)
    assert re.search(r'variable "maximum_capacity"[\s\S]+?default\s+=\s+3', variables)
    assert "var.maximum_capacity <= 5" in main
    assert 'http_tokens                 = "required"' in main
    assert 'http_put_response_hop_limit = 1' in main
    assert "encrypted             = true" in main
    assert 'volume_type           = "gp3"' in main
    assert "var.desired_capacity <= var.maximum_capacity" in main
    assert "subnet.vpc_id == var.vpc_id" in main


def test_runtime_iam_scopes_mutation_and_has_no_resource_lifecycle_actions() -> None:
    iam = read("infra/iam.tf")

    assert 'Action   = ["autoscaling:SetDesiredCapacity"]' in iam
    assert "Resource = [local.target_asg_arn]" in iam
    assert "autoScalingGroupName/%s" in iam
    assert 'Action   = ["sqs:GetQueueAttributes", "sqs:GetQueueUrl"]' in iam
    assert "Resource = [var.sqs_queue_arn]" in iam
    assert '"cloudwatch:namespace" = var.pulse_metric_namespace' in iam
    forbidden = (
        "autoscaling:Create",
        "autoscaling:Delete",
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "sqs:CreateQueue",
        "sqs:DeleteQueue",
        "iam:Create",
        "iam:Delete",
    )
    assert not any(action in iam for action in forbidden)


def test_comparator_outputs_examples_and_generated_files_are_safe() -> None:
    monitoring = read("infra/monitoring.tf")
    outputs = read("infra/outputs.tf")
    example = read("infra/environments/demo.tfvars.example")
    ignores = read(".gitignore")

    assert re.search(r"namespace\s+=\s+var\.pulse_metric_namespace", monitoring)
    assert re.search(r'metric_name\s+=\s+"ReactiveComparator"', monitoring)
    assert re.search(
        r"actions_enabled\s+=\s+length\(var\.reactive_alarm_action_arns\) > 0",
        monitoring,
    )
    for output_name in (
        "autoscaling_group_name",
        "autoscaling_group_arn",
        "reactive_comparator_alarm_name",
        "runtime_role_arn",
        "runtime_policy_arn",
    ):
        assert f'output "{output_name}"' in outputs
    assert "minimum_capacity            = 1" in example
    assert "desired_capacity            = 1" in example
    assert "maximum_capacity            = 3" in example
    assert not re.search(r"\b\d{12}\b", example)
    assert "*.tfstate" in ignores
    assert "*.tfplan" in ignores
    assert "*.tfvars" in ignores
    assert "!*.tfvars.example" in ignores
    assert ".coverage.*" in ignores


def test_every_terraform_source_is_nonempty_and_balanced() -> None:
    sources = sorted(INFRA.glob("*.tf"))
    assert {path.name for path in sources} == {
        "iam.tf",
        "main.tf",
        "monitoring.tf",
        "outputs.tf",
        "variables.tf",
        "versions.tf",
    }
    for source in sources:
        value = source.read_text(encoding="utf-8")
        assert value.strip()
        assert value.count("{") == value.count("}"), source
        assert "TODO" not in value
