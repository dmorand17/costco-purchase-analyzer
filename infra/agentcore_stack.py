import json

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_ses as ses
from aws_cdk import custom_resources as cr
from constructs import Construct

from infra.common_stack import CommonStack


class AgentCoreStack(Stack):
    """
    Deploys the weekly price-check agent using Amazon Bedrock AgentCore Runtime.
    Only instantiated when --context notifyEmail=<address> is provided.

    The AgentCore Runtime is created via AwsCustomResource (SDK call) because
    a stable CDK L2 construct for this service is not yet available.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        common: CommonStack,
        notify_email: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── IAM Role for AgentCore Runtime ─────────────────────────────────
        agentcore_role = iam.Role(
            self,
            "AgentCoreRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )

        common.receipts_table.grant_read_write_data(agentcore_role)
        common.price_drops_table.grant_read_write_data(agentcore_role)
        common.receipts_bucket.grant_read_write(agentcore_role)

        agentcore_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=["*"],
            )
        )
        agentcore_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"],
            )
        )
        agentcore_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        common.agentcore_ecr_repo.grant_pull(agentcore_role)

        # ── Docker image for AgentCore ─────────────────────────────────────
        image = ecr_assets.DockerImageAsset(
            self,
            "AgentCoreImage",
            directory=".",
            file="agentcore.Dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # ── AgentCore Runtime (via SDK custom resource) ────────────────────
        # Replace this with the official CDK L2 construct when available.
        env_vars = {
            "DYNAMODB_RECEIPTS_TABLE": common.receipts_table.table_name,
            "DYNAMODB_PRICE_DROPS_TABLE": common.price_drops_table.table_name,
            "S3_BUCKET": common.receipts_bucket.bucket_name,
            "NOTIFY_EMAIL": notify_email,
            "AWS_DEFAULT_REGION": self.region,
        }

        runtime_name = "costco_purchase_analyzer"

        agent_runtime = cr.AwsCustomResource(
            self,
            "AgentCoreRuntime",
            on_create=cr.AwsSdkCall(
                service="bedrock-agentcore",
                action="CreateAgentRuntime",
                parameters={
                    "agentRuntimeName": runtime_name,
                    "agentRuntimeArtifact": {
                        "containerConfiguration": {
                            "containerUri": image.image_uri,
                        }
                    },
                    "roleArn": agentcore_role.role_arn,
                    "environmentVariables": env_vars,
                },
                physical_resource_id=cr.PhysicalResourceId.from_response(
                    "agentRuntimeArn"
                ),
            ),
            on_delete=cr.AwsSdkCall(
                service="bedrock-agentcore",
                action="DeleteAgentRuntime",
                parameters={
                    "agentRuntimeId": cr.PhysicalResourceIdReference(),
                },
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:*"],
                        resources=["*"],
                    )
                ]
            ),
        )

        runtime_arn = agent_runtime.get_response_field("agentRuntimeArn")

        # ── SES Email Identity ─────────────────────────────────────────────
        ses.EmailIdentity(
            self,
            "NotifyEmailIdentity",
            identity=ses.Identity.email(notify_email),
        )

        # ── IAM Role for EventBridge Scheduler ────────────────────────────
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=["*"],
            )
        )

        # ── EventBridge Scheduler: every Friday at 9pm ET ─────────────────
        scheduler.CfnSchedule(
            self,
            "WeeklySchedule",
            name="costco-purchase-analyzer-weekly",
            schedule_expression="cron(0 21 ? * FRI *)",
            schedule_expression_timezone="America/New_York",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF"
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn="arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime",
                role_arn=scheduler_role.role_arn,
                input=json.dumps(
                    {
                        "AgentRuntimeArn": runtime_arn,
                        "Payload": {"prompt": "run weekly scan"},
                    }
                ),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_retry_attempts=0,
                ),
            ),
        )

        # ── Outputs ────────────────────────────────────────────────────────
        CfnOutput(self, "AgentCoreRuntimeArn", value=runtime_arn)
        CfnOutput(self, "NotifyEmail", value=notify_email)
