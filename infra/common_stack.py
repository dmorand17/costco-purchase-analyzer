from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_s3 as s3
from constructs import Construct


class CommonStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.receipts_table = dynamodb.Table(
            self,
            "ReceiptsTable",
            table_name="CostcoReceipts",
            partition_key=dynamodb.Attribute(
                name="receipt_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.price_drops_table = dynamodb.Table(
            self,
            "PriceDropsTable",
            table_name="CostcoPriceDrops",
            partition_key=dynamodb.Attribute(
                name="item_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.receipts_bucket = s3.Bucket(
            self,
            "ReceiptsBucket",
            removal_policy=RemovalPolicy.RETAIN,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        self.lambda_ecr_repo = ecr.Repository(
            self,
            "LambdaEcrRepo",
            repository_name="costco-purchase-analyzer-lambda",
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.agentcore_ecr_repo = ecr.Repository(
            self,
            "AgentCoreEcrRepo",
            repository_name="costco-purchase-analyzer-agentcore",
            removal_policy=RemovalPolicy.RETAIN,
        )

        CfnOutput(self, "ReceiptsTableName", value=self.receipts_table.table_name)
        CfnOutput(self, "PriceDropsTableName", value=self.price_drops_table.table_name)
        CfnOutput(self, "ReceiptsBucketName", value=self.receipts_bucket.bucket_name)
        CfnOutput(
            self,
            "LambdaEcrRepoUri",
            value=self.lambda_ecr_repo.repository_uri,
        )
        CfnOutput(
            self,
            "AgentCoreEcrRepoUri",
            value=self.agentcore_ecr_repo.repository_uri,
        )
