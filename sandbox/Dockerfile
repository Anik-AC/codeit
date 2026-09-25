# CodeIt worker image (PRD 10.1). Build with `codeit sandbox build`.
#
# Playwright's image provides Node LTS, browsers, Python 3.12 and git. The Playwright
# version must match what target repos pin (@playwright/test 1.63.0 in M4.5).
FROM mcr.microsoft.com/playwright:v1.63.0-noble

ARG CLAUDE_CODE_VERSION=2.1.282
ARG OPENCODE_VERSION=1.18.32
ARG UV_VERSION=0.12.19

ENV DEBIAN_FRONTEND=noninteractive

# gh from GitHub's apt repository; jq and Python tooling from Ubuntu.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg jq python3-venv python3-pip \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "opencode-ai@${OPENCODE_VERSION}" \
 && npm cache clean --force

RUN curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" | env UV_INSTALL_DIR=/usr/local/bin sh

# Non-root user `agent` with UID 1000 (Ubuntu's default `ubuntu` user, renamed), so files
# written to the bind-mounted clone belong to the owner's UID on the host.
RUN usermod -l agent -d /home/agent -m ubuntu && groupmod -n agent ubuntu && usermod -G "" agent

# Commits are authored by the bot (PRD D5). Pushes authenticate through gh with GH_TOKEN.
RUN git config --system user.name "codeit-bot" \
 && git config --system user.email "bot@users.noreply.github.com" \
 && git config --system credential.https://github.com.helper "" \
 && git config --system --add credential.https://github.com.helper "!gh auth git-credential" \
 && git config --system init.defaultBranch main

ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

LABEL org.opencontainers.image.title="codeit-worker" \
      org.opencontainers.image.source="https://github.com/Anik-AC/codeit"

USER agent
WORKDIR /workspace
CMD ["sleep", "infinity"]
