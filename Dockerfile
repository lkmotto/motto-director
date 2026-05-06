FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# Install Node.js + Anthropic Claude Code CLI so the claude_max provider can
# call the LLM as a subprocess (Anthropic blocked direct OAuth /v1/messages
# calls in Jan 2026; the CLI is the supported path for Claude Max billing).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md motto-strategy.md ./
COPY director ./director

RUN uv pip install --system .

CMD ["python", "-m", "director.main"]
