#!/usr/bin/env python3
import aws_cdk as cdk
from infra.common_stack import CommonStack
from infra.amplify_stack import AmplifyStack
from infra.agentcore_stack import AgentCoreStack

app = cdk.App()

region = app.node.try_get_context("region") or "us-east-1"
notify_email = app.node.try_get_context("notifyEmail")

env = cdk.Environment(region=region)

common = CommonStack(app, "CostcoPurchaseAnalyzerCommon", env=env)
amplify = AmplifyStack(app, "CostcoPurchaseAnalyzerAmplify", common=common, env=env)

if notify_email:
    AgentCoreStack(
        app,
        "CostcoPurchaseAnalyzerAgentCore",
        common=common,
        notify_email=notify_email,
        env=env,
    )

app.synth()
