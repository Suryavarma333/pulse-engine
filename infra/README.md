# Pulse low-cost AWS demonstration infrastructure

This Terraform root creates only the small AWS boundary needed to demonstrate Pulse: an
IMDSv2-only launch template, an Auto Scaling Group (ASG), a CloudWatch reactive-comparator alarm,
and the EC2 runtime role/policy. It deliberately reuses existing VPC, subnet, security-group, and
AMI inputs. It creates no VPC, NAT gateway, load balancer, database, queue, or DNS record.

Terraform and the runtime are separate safety layers. The ASG starts at one instance and the
example maximum is three. Pulse still defaults to `dry_run`; applying this stack does not enable
live capacity changes. To use the adapter live, an operator must separately set
`PULSE_EXECUTION_MODE=live`, `AWS_REGION`, `PULSE_ASG_NAME`, and an application ceiling no larger
than the Terraform ASG maximum.

## Prerequisites and inputs

- Terraform 1.6 or newer and AWS provider credentials supplied by the standard AWS credential
  chain. Never write credentials into a `.tfvars` file.
- An existing VPC, one or more subnets, security groups with reviewed ingress/egress, and an AMI
  compatible with the selected low-cost instance type.
- Account quota and regional availability for the selected instance type.

Copy the example without committing the copy:

```bash
cp infra/environments/demo.tfvars.example infra/environments/demo.auto.tfvars
```

Replace every example resource ID. Keep `associate_public_ip_address = false` for private
subnets. Setting it to true is the optional lowest-cost direct-access behavior, but it does not
provide a stable endpoint and the supplied security group remains responsible for all exposure.

## Format, initialize, and review

```bash
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
terraform -chdir=infra plan -var-file=environments/demo.auto.tfvars -out=pulse-demo.tfplan
terraform -chdir=infra show pulse-demo.tfplan
```

The plan file and local variable copy are ignored. Review the instance count, AMI, networking,
security groups, tags, and IAM policy before any apply. CI performs format/init/validate only and
never receives AWS credentials or runs apply.

## Explicit apply and destroy

Option 1 delivery does not run these commands. An authorized operator may apply the reviewed
plan and should destroy the short-lived demo immediately afterward:

```bash
terraform -chdir=infra apply pulse-demo.tfplan
terraform -chdir=infra output
terraform -chdir=infra plan -destroy -var-file=environments/demo.auto.tfvars
terraform -chdir=infra destroy -var-file=environments/demo.auto.tfvars
```

Confirm that the ASG, launch template, alarm, IAM role, policy, and instance profile are gone.
Terraform state can contain operational identifiers and must stay in an approved backend or
uncommitted local storage.

## IAM rationale

`autoscaling:SetDesiredCapacity` is scoped to the deterministic target ASG ARN. Optional SQS
reads are scoped to the one supplied queue ARN. AWS does not support resource-level permissions
for `autoscaling:DescribeAutoScalingGroups`, the CloudWatch metric read APIs, or
`cloudwatch:PutMetricData`; those statements require `Resource: "*"`. Metric publication is
therefore constrained by a `cloudwatch:namespace = Pulse/Traffic` condition. The policy has no
resource create, update-definition, terminate, or delete permissions.

The CloudWatch alarm observes the `Pulse/Traffic` / `ReactiveComparator` 0-or-1 metric and has no
action by default. Optional action ARNs must already exist and are capped at two. Pulse uses the
alarm crossing only as the reactive baseline for detection-lead evidence; the default dry-run
response path remains credential-free.
