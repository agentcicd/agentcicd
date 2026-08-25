FROM python:3.13-slim AS production

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /src

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
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

COPY agentcicd_contacts /src/agentcicd_contacts
COPY agentcicd_dp_common /src/agentcicd_dp_common
COPY agentcicd /src/agentcicd

EXPOSE 18081

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "agentcicd.sandbox.gvisor_helper"]
