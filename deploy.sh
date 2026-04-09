#!/usr/bin/env bash
# deploy.sh — Build, push, and deploy the Costco Purchase Analyzer
#
# Usage:
#   ./deploy.sh [--region us-east-1] [--notify-email your@email.com]
#
# Prerequisites:
#   - uv (https://docs.astral.sh/uv/)
#   - AWS CLI configured with deploy permissions
#   - Docker running
#   - CDK bootstrapped: uv run --group dev cdk bootstrap

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
NOTIFY_EMAIL=""

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --notify-email) NOTIFY_EMAIL="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "▶ Deploying to account=${ACCOUNT_ID} region=${REGION}"

# ── CDK context args ──────────────────────────────────────────────────────────
CDK_ARGS="-c region=${REGION}"
if [[ -n "${NOTIFY_EMAIL}" ]]; then
  CDK_ARGS="${CDK_ARGS} -c notifyEmail=${NOTIFY_EMAIL}"
fi

# ── Install CDK dependencies ──────────────────────────────────────────────────
echo "▶ Installing dependencies..."
uv sync --group dev

# ── Deploy CDK stacks ─────────────────────────────────────────────────────────
echo "▶ Deploying CDK stacks..."
uv run --group dev cdk deploy --all --require-approval never ${CDK_ARGS}

# ── Read stack outputs ────────────────────────────────────────────────────────
echo "▶ Reading stack outputs..."

get_output() {
  local stack="$1"
  local key="$2"
  aws cloudformation describe-stacks \
    --stack-name "${stack}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output text
}

AMPLIFY_APP_ID=$(get_output "CostcoPurchaseAnalyzerAmplify" "AmplifyAppId")
API_URL=$(get_output "CostcoPurchaseAnalyzerAmplify" "ApiUrl")
USER_POOL_ID=$(get_output "CostcoPurchaseAnalyzerAmplify" "UserPoolId")
WEB_CLIENT_ID=$(get_output "CostcoPurchaseAnalyzerAmplify" "WebAppClientId")

echo "  API URL:        ${API_URL}"
echo "  User Pool ID:   ${USER_POOL_ID}"
echo "  Web Client ID:  ${WEB_CLIENT_ID}"
echo "  Amplify App ID: ${AMPLIFY_APP_ID}"

# ── Generate config.js ────────────────────────────────────────────────────────
echo "▶ Generating static/config.js..."
cat > static/config.js << EOF
window.APP_CONFIG = {
  API_URL: "${API_URL}",
  COGNITO_USER_POOL_ID: "${USER_POOL_ID}",
  COGNITO_CLIENT_ID: "${WEB_CLIENT_ID}",
  REGION: "${REGION}"
};
EOF

# Inject config into index.html for Amplify (since Amplify serves static files)
# Embed APP_CONFIG inline to avoid a separate config.js request
CONFIG_JSON="{\"API_URL\":\"${API_URL}\",\"COGNITO_USER_POOL_ID\":\"${USER_POOL_ID}\",\"COGNITO_CLIENT_ID\":\"${WEB_CLIENT_ID}\",\"REGION\":\"${REGION}\"}"
sed "s|window.APP_CONFIG = window.APP_CONFIG || window.CONFIG || {};|window.APP_CONFIG = ${CONFIG_JSON};|g" \
  static/index.html > /tmp/index_configured.html

# ── Deploy to Amplify ─────────────────────────────────────────────────────────
echo "▶ Deploying frontend to Amplify..."
TMP_ZIP="/tmp/costco-frontend.zip"
cp /tmp/index_configured.html static/index.html
zip -j "${TMP_ZIP}" static/index.html

DEPLOYMENT=$(aws amplify create-deployment \
  --app-id "${AMPLIFY_APP_ID}" \
  --branch-name "main" \
  --region "${REGION}" \
  --output json)

UPLOAD_URL=$(echo "${DEPLOYMENT}" | python3 -c "import sys,json; print(json.load(sys.stdin)['zipUploadUrl'])")
JOB_ID=$(echo "${DEPLOYMENT}" | python3 -c "import sys,json; print(json.load(sys.stdin)['jobId'])")

curl -s -X PUT -H "Content-Type: application/zip" --data-binary @"${TMP_ZIP}" "${UPLOAD_URL}"

aws amplify start-deployment \
  --app-id "${AMPLIFY_APP_ID}" \
  --branch-name "main" \
  --job-id "${JOB_ID}" \
  --region "${REGION}" \
  --no-cli-pager

AMPLIFY_URL=$(get_output "CostcoPurchaseAnalyzerAmplify" "AmplifyAppUrl")
echo ""
echo "✅ Deployment complete!"
echo "   App URL: ${AMPLIFY_URL}"
echo "   API URL: ${API_URL}"
if [[ -n "${NOTIFY_EMAIL}" ]]; then
  echo "   Weekly report will be sent to: ${NOTIFY_EMAIL}"
  echo "   ⚠️  Check your email to verify the SES identity before the first run."
fi
