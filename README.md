# Costco Purchase Analyzer

An AI-powered Costco receipt scanner and price match tool. Upload your Costco receipt PDFs, let Amazon Bedrock Nova parse every line item, and have an AI agent cross-reference your purchases against active deals to identify price adjustment opportunities you can claim at the membership counter.

> **Inspired by** the Medium article [*"I've Been a Costco Member for 25 Years — Last Month I Built an AI Agent to Get My Money Back"*](https://aws.plainenglish.io/ive-been-a-costco-member-for-25-years-last-month-i-built-an-ai-agent-to-get-my-money-back-09ed903751b0) and the original TypeScript CDK implementation at [awsdataarchitect/costco-price-match](https://github.com/awsdataarchitect/costco-price-match).
>
> This repository is a Python re-implementation using `uv` for package management, Python CDK for infrastructure, and Strands Agents for the AI analysis layer.

---

## Features

- **Receipt parsing** — Upload Costco receipt PDFs; Amazon Nova 2 Lite extracts every line item with item numbers, quantities, prices, and instant savings (TPD) flags
- **High-accuracy re-parse** — Optionally re-parse with Amazon Nova Premier using a multi-prompt image approach for complex receipts
- **Deal scraping** — Pulls active deals from 7 sources: CocoWest, CocoEast, Costco Coupon Book (AI-parsed), RedFlagDeals Hot Deals, RedFlagDeals Clearance, Reddit r/Costco, Reddit r/CostcoCanada
- **AI analysis** — A Strands Agent matches your purchases against deals, distinguishes already-discounted items (TPD), and streams results in real time
- **Weekly email report** — An AgentCore Runtime job runs every Friday at 9pm ET, refreshes all deals, analyzes your receipts, and sends an HTML report via SES
- **Web UI** — Single-page app hosted on AWS Amplify with Cognito authentication

---

## Architecture

```
┌─────────────┐     PDF upload      ┌─────────────────────────────────┐
│   Browser   │ ──────────────────► │  API Gateway HTTP API (JWT)     │
│  (Amplify)  │ ◄────── SSE stream  │  Lambda (FastAPI + Mangum)      │
└─────────────┘                     │  ARM64, 1024 MB, 5 min timeout  │
      │ Cognito auth                 └────────────┬────────────────────┘
      │                                           │
      ▼                                    ┌──────┴──────┐
┌──────────────┐                           │   Services  │
│   Cognito    │                           ├─────────────┤
│  User Pool   │                           │ DynamoDB    │ ← Receipts + Price Drops
└──────────────┘                           │ S3          │ ← Receipt PDFs
                                           │ Bedrock     │ ← Nova Lite / Premier
                                           │ Strands     │ ← Analysis agent
                                           └─────────────┘

EventBridge Scheduler (Fri 9pm ET)
    │
    ▼
AgentCore Runtime (Docker, AMD64)
    │ scrape → analyze → email
    ▼
Amazon SES → your inbox
```

### CDK Stacks

| Stack | Resources |
|-------|-----------|
| `CostcoPurchaseAnalyzerCommon` | DynamoDB (2 tables), S3 bucket, ECR (2 repos) |
| `CostcoPurchaseAnalyzerAmplify` | Cognito User Pool, Lambda, API Gateway, Amplify hosting |
| `CostcoPurchaseAnalyzerAgentCore` | AgentCore Runtime, SES identity, EventBridge Scheduler |

The AgentCore stack is only deployed when `--context notifyEmail=<address>` is provided.

---

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- AWS CLI configured with appropriate permissions
- Docker (for CDK Docker asset builds)
- CDK CLI — `npm install -g aws-cdk` (or use `uvx aws-cdk`)

---

## Getting Started

### 1. Install dependencies

```bash
# Runtime + dev (CDK) deps
uv sync --group dev
```

### 2. Bootstrap CDK (first time only)

```bash
uv run --group dev cdk bootstrap aws://ACCOUNT_ID/REGION
```

### 3. Deploy

```bash
# Basic deployment (web app only, no weekly email)
./deploy.sh --region us-east-1

# With weekly email reports
./deploy.sh --region us-east-1 --notify-email your@email.com
```

The deploy script:
1. Deploys all CDK stacks (builds Docker images, provisions all AWS resources)
2. Reads CloudFormation outputs
3. Embeds Cognito config into the frontend
4. Uploads the frontend to Amplify

### 4. Access the app

The Amplify URL is printed at the end of deployment. Sign up for an account on the login screen.

---

## Local Development

```bash
# Copy and fill in values from your deployed stack
cp .env.example .env

# Start the local server (auto-reloads)
uv run uvicorn app:app --reload --port 8000
```

`.env.example`:
```
DYNAMODB_RECEIPTS_TABLE=CostcoReceipts
DYNAMODB_PRICE_DROPS_TABLE=CostcoPriceDrops
S3_BUCKET=your-bucket-name
AWS_DEFAULT_REGION=us-east-1
USER_POOL_ID=
USER_POOL_CLIENT_ID=
```

Open `http://localhost:8000` — the app runs without Cognito auth in local mode.

---

## Project Structure

```
.
├── cdk_app.py                  # CDK app entry point
├── app.py                      # FastAPI application (Lambda handler)
├── agent.py                    # AgentCore weekly agent entry point
├── pyproject.toml              # uv project: runtime deps + CDK dev group
├── cdk.json                    # CDK configuration
├── lambda.Dockerfile           # Lambda container image
├── agentcore.Dockerfile        # AgentCore container image
├── deploy.sh                   # Full deployment script
├── infra/
│   ├── common_stack.py         # DynamoDB, S3, ECR
│   ├── amplify_stack.py        # Cognito, Lambda, API Gateway, Amplify
│   └── agentcore_stack.py      # AgentCore Runtime, SES, EventBridge Scheduler
├── services/
│   ├── db.py                   # DynamoDB + S3 data layer
│   ├── receipt_parser.py       # Bedrock Nova receipt parsing
│   ├── price_scanner.py        # 7-source deal scraper
│   └── analyzer.py             # Strands Agent + SSE streaming
└── static/
    └── index.html              # Single-file web frontend
```

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `POST` | `/api/upload` | Upload and parse a receipt PDF |
| `POST` | `/api/reparse/{id}` | Re-parse with Nova Premier |
| `GET` | `/api/receipts` | List all receipts |
| `DELETE` | `/api/receipts` | Delete all receipts |
| `DELETE` | `/api/receipt/{id}` | Delete one receipt |
| `GET` | `/api/receipt/{id}/pdf` | Download receipt PDF |
| `PUT` | `/api/receipt/{id}/item/{idx}` | Edit a line item |
| `POST` | `/api/scan-prices` | Scrape all deal sources |
| `GET` | `/api/price-drops` | List stored deals |
| `DELETE` | `/api/price-drops` | Clear all deals |
| `GET` | `/api/analyze` | SSE streaming AI analysis |

All endpoints except `/` require a valid Cognito JWT (`Authorization: Bearer <token>`).

---

## Deal Sources

| Source | Description |
|--------|-------------|
| `cocowest` | CocoWest.ca in-store instant savings |
| `cocoeast` | CocoEast.ca in-store instant savings |
| `coupon_book` | Costco coupon book pages (AI-parsed via SmartCanucks) |
| `redflagdeals_hot` | RedFlagDeals Costco hot deals forum |
| `redflagdeals_clearance` | RedFlagDeals $.97 clearance thread |
| `reddit_costco` | Reddit r/Costco deal posts |
| `reddit_costcoca` | Reddit r/CostcoCanada deal posts |

---

## Costco Price Adjustment Policy

Costco allows members to request a price adjustment within **30 days of purchase** if an item goes on sale. Visit the membership counter with your receipt. Items that already had an Instant Savings coupon (TPD) applied are **not eligible** — the analyzer flags these separately.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Infrastructure | AWS CDK v2 (Python) |
| Package management | [uv](https://docs.astral.sh/uv/) |
| API | FastAPI + Mangum (ASGI → Lambda) |
| AI models | Amazon Bedrock Nova 2 Lite, Nova Premier |
| AI agent | [Strands Agents SDK](https://github.com/strands-agents/sdk-python) |
| Weekly job | Amazon Bedrock AgentCore Runtime |
| Auth | Amazon Cognito User Pools |
| Storage | DynamoDB (pay-per-request), S3 |
| Email | Amazon SES |
| Hosting | AWS Amplify |

---

## License

MIT
