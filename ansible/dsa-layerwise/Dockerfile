ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SOURCE_REVISION
ARG PACKAGE_ROOT=/usr/local/python3.12.13/lib/python3.12/site-packages/vllm_ascend
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}" \
      glm52.deployment="layerwise-guide"
COPY vllm_ascend/ ${PACKAGE_ROOT}/
COPY examples/disaggregated_prefill_v1/ /vllm-workspace/vllm-ascend/examples/disaggregated_prefill_v1/
COPY SOURCE_REVISION SOURCE_SHA256SUMS /opt/layerwise-guide/
RUN cd / && sha256sum --check /opt/layerwise-guide/SOURCE_SHA256SUMS >/dev/null && \
    python3 -m compileall -q "${PACKAGE_ROOT}" && \
    python3 -c 'import os; from importlib.util import find_spec; assert find_spec("vllm_ascend").origin == os.environ["PACKAGE_ROOT"] + "/__init__.py"'
