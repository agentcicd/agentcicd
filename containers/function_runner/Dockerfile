FROM python:3.13-slim AS deps

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

ARG NODE_MAJOR=22
ARG CODEX_CLI_VERSION=0.142.5
ARG CLAUDE_CODE_VERSION=2.1.198
ARG PLAYWRIGHT_MCP_VERSION=0.0.77

WORKDIR /src

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
	    curl \
	    docker-cli \
	    git \
        gnupg \
        jq \
        libgomp1 \
        procps \
        tini \
        unzip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY agentcicd_contacts/pyproject.toml ./agentcicd_contacts/pyproject.toml
COPY agentcicd_dp_common/pyproject.toml ./agentcicd_dp_common/pyproject.toml
COPY agentcicd/pyproject.toml ./agentcicd/pyproject.toml

RUN for pkg in agentcicd_contacts agentcicd_dp_common agentcicd; do \
        mkdir -p ${pkg}/src/${pkg} && \
        touch ${pkg}/src/${pkg}/__init__.py && \
        touch ${pkg}/README.md; \
    done

RUN pip install --no-cache-dir \
    -e ./agentcicd_contacts/ \
    -e ./agentcicd_dp_common/ \
    -e "./agentcicd[fixtures]"

RUN pip install --no-cache-dir \
        ipykernel \
        jupyterlab \
        matplotlib \
        nbclient \
        nbformat \
        numpy \
        openpyxl \
        pandas \
        pillow \
        plotly \
        pyxlsb \
        scikit-learn \
        scipy \
        seaborn \
        statsmodels \
        xlrd

RUN npm install -g \
        @openai/codex@${CODEX_CLI_VERSION} \
        @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
        @playwright/mcp@${PLAYWRIGHT_MCP_VERSION}

RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/agentcicd-function-runner \
    && printf '{\n  "python": "%s",\n  "node": "%s",\n  "jupyterlab": "%s",\n  "pandas": "%s",\n  "scikit_learn": "%s",\n  "playwright": "%s",\n  "chromium": "playwright-chromium",\n  "codex": "%s",\n  "claude": "%s",\n  "playwright_mcp": "%s"\n}\n' \
        "$(python --version | awk '{print $2}')" \
        "$(node --version)" \
        "$(python -c 'import importlib.metadata; print(importlib.metadata.version("jupyterlab"))')" \
        "$(python -c 'import importlib.metadata; print(importlib.metadata.version("pandas"))')" \
        "$(python -c 'import importlib.metadata; print(importlib.metadata.version("scikit-learn"))')" \
        "$(python -c 'import importlib.metadata; print(importlib.metadata.version("playwright"))')" \
        "$(codex --version | awk '{print $2}')" \
        "$(claude --version | awk '{print $1}')" \
        "$(playwright-mcp --version | awk '{print $1}')" \
        > /opt/agentcicd-function-runner/tool-versions.json

FROM deps AS production

WORKDIR /app
COPY agentcicd_contacts /src/agentcicd_contacts
COPY agentcicd_dp_common /src/agentcicd_dp_common
COPY agentcicd /src/agentcicd

RUN groupadd --system agentcicd \
    && useradd --system --gid agentcicd --home-dir /home/agentcicd --create-home --shell /usr/sbin/nologin agentcicd \
    && mkdir -p /home/agentcicd/.codex /home/agentcicd/.claude /workspace \
    && chown -R agentcicd:agentcicd /home/agentcicd /workspace /app

ENV HOME=/home/agentcicd
USER agentcicd

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "agentcicd.sandbox.function_runner"]
