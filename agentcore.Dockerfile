FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies (main group only)
COPY pyproject.toml .
RUN uv pip install --system \
    "fastapi>=0.115.0" \
    "mangum>=0.19.0" \
    "boto3>=1.38.0" \
    "strands-agents>=0.1.7" \
    "beautifulsoup4>=4.12.0" \
    "requests>=2.32.0" \
    "pillow>=11.0.0" \
    "pymupdf>=1.25.0" \
    "playwright>=1.49.0" \
    "python-multipart>=0.0.12" \
    "markdown2>=2.5.0" \
    "uvicorn[standard]>=0.34.0" \
    "bedrock-agentcore>=0.1.0"

# Install Playwright Chromium with system deps
RUN playwright install --with-deps chromium

# Add non-root user (required by AgentCore Runtime)
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

# Copy application source
COPY --chown=bedrock_agentcore:bedrock_agentcore agent.py ./
COPY --chown=bedrock_agentcore:bedrock_agentcore services/ services/

EXPOSE 8080

CMD ["python", "-m", "agent"]
