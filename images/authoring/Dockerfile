# Authoring worker image. Keep the requested base image in config/images.yaml.
FROM harbor-eval/qwen-code:0.25.1 AS cli
FROM orginalbench:trajectory-runtime-v1

USER root
COPY --from=cli /usr/local/bin/ /opt/qwen-runtime/bin/
COPY --from=cli /usr/local/lib/node_modules/ /opt/qwen-runtime/lib/node_modules/
ENV PATH="/opt/qwen-runtime/bin:${PATH}"
RUN for tool in node npm npx qwen; do \
      ln -sf "/opt/qwen-runtime/bin/$tool" "/usr/local/bin/$tool"; \
    done \
    && node --version \
    && npm --version \
    && test "$(qwen --version)" = "0.25.1"
ENTRYPOINT []
WORKDIR /workspace
CMD ["bash"]
