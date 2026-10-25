# hermes-bridge container image. Standard library only, so the image is just Python + this package.
# Run it on the same host (or network namespace) as a loopback-only `hermes serve`; see README.
FROM python:3.12-slim
WORKDIR /src
COPY pyproject.toml README.md LICENSE.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /src \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin bridge
USER bridge
ENV HERMES_BRIDGE_LISTEN=0.0.0.0:8765 \
    HERMES_BRIDGE_KEY_FILE=/run/secrets/hermes_bridge_key \
    HERMES_BRIDGE_DASHBOARD_TOKEN_FILE=/run/secrets/hermes_dashboard_token
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;h,p=os.environ['HERMES_BRIDGE_LISTEN'].rsplit(':',1);urllib.request.urlopen(f'http://127.0.0.1:{p}/v1/health',timeout=4)"
ENTRYPOINT ["hermes-bridge"]
CMD ["serve"]
