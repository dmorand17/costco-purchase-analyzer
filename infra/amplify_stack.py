from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_amplify as amplify
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from infra.common_stack import CommonStack


class AmplifyStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        common: CommonStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Cognito User Pool ──────────────────────────────────────────────
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="costco-purchase-analyzer-users",
            sign_in_aliases=cognito.SignInAliases(email=True),
            self_sign_up_enabled=True,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Web client only (no iOS)
        web_client = user_pool.add_client(
            "WebClient",
            user_pool_client_name="costco-purchase-analyzer-web",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_srp=True,
                user_password=True,
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
        )

        # ── Lambda (Docker) ────────────────────────────────────────────────
        api_function = lambda_.DockerImageFunction(
            self,
            "ApiFunction",
            function_name="costco-purchase-analyzer-api",
            code=lambda_.DockerImageCode.from_image_asset(
                ".",
                file="lambda.Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            architecture=lambda_.Architecture.ARM_64,
            memory_size=1024,
            timeout=Duration.seconds(300),
            environment={
                "DYNAMODB_RECEIPTS_TABLE": common.receipts_table.table_name,
                "DYNAMODB_PRICE_DROPS_TABLE": common.price_drops_table.table_name,
                "S3_BUCKET": common.receipts_bucket.bucket_name,
                "USER_POOL_ID": user_pool.user_pool_id,
                "USER_POOL_CLIENT_ID": web_client.user_pool_client_id,
            },
        )

        # Permissions
        common.receipts_table.grant_read_write_data(api_function)
        common.price_drops_table.grant_read_write_data(api_function)
        common.receipts_bucket.grant_read_write(api_function)

        api_function.add_to_role_policy(
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
        api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"],
            )
        )
        api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:ListTables"],
                resources=["*"],
            )
        )

        # ── API Gateway HTTP API ───────────────────────────────────────────
        http_api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name="costco-purchase-analyzer-api",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigw.CorsHttpMethod.ANY],
                allow_headers=["Authorization", "Content-Type"],
                max_age=Duration.seconds(3600),
            ),
        )

        jwt_authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[web_client.user_pool_client_id],
        )

        lambda_integration = integrations.HttpLambdaIntegration(
            "LambdaIntegration",
            handler=api_function,
        )

        # All authenticated routes
        http_api.add_routes(
            path="/{proxy+}",
            methods=[apigw.HttpMethod.ANY],
            integration=lambda_integration,
            authorizer=jwt_authorizer,
        )

        # CORS preflight (unauthenticated)
        http_api.add_routes(
            path="/{proxy+}",
            methods=[apigw.HttpMethod.OPTIONS],
            integration=lambda_integration,
        )

        # ── Amplify Hosting ────────────────────────────────────────────────
        amplify_app = amplify.CfnApp(
            self,
            "AmplifyApp",
            name="costco-purchase-analyzer",
            platform="WEB",
            environment_variables=[
                amplify.CfnApp.EnvironmentVariableProperty(
                    name="_LIVE_UPDATES",
                    value='[{"name":"Amplify CLI","pkg":"@aws-amplify/cli","type":"npm","version":"latest"}]',
                )
            ],
        )

        amplify.CfnBranch(
            self,
            "AmplifyMainBranch",
            app_id=amplify_app.attr_app_id,
            branch_name="main",
            enable_auto_build=False,
        )

        # ── Outputs ────────────────────────────────────────────────────────
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "WebAppClientId", value=web_client.user_pool_client_id)
        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "AmplifyAppId", value=amplify_app.attr_app_id)
        CfnOutput(
            self,
            "AmplifyAppUrl",
            value=f"https://main.{amplify_app.attr_default_domain}",
        )

        self.api_url = http_api.api_endpoint
        self.user_pool = user_pool
        self.web_client = web_client
        self.amplify_app = amplify_app
