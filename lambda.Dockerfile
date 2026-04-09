FROM public.ecr.aws/lambda/python:3.12

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Use system Python managed by uv
ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR ${LAMBDA_TASK_ROOT}

# Install Python dependencies (main group only, not dev)
COPY pyproject.toml .
RUN uv pip install --system --no-dev -e . 2>/dev/null || \
    uv pip install --system \
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
        "markdown2>=2.5.0"

# Install Playwright Chromium browser
RUN python -m playwright install chromium
RUN python -m playwright install-deps chromium

# Copy application source
COPY app.py agent.py ./
COPY services/ services/
COPY static/ static/

CMD ["app.handler"]
