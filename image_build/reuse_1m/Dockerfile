FROM glm52-vllm-ascend:0.30.0-main-reuse-dev-20261103-r03
COPY enable_memfabric_protocol_export.py /root/enable_memfabric_protocol_export.py
RUN python3 /root/enable_memfabric_protocol_export.py && \
    python3 -m py_compile /usr/local/python3.12.13/lib/python3.12/site-packages/memfabric_hybrid/__init__.py && \
    ln -s /mnt/sfs_turbo/models/GLM-5.2-w4a8c8 /root/reuse-main-20261103-1m-model
