# syntax=docker/dockerfile:1
FROM rust:slim-bookworm@sha256:86234e857a413004348b471d11a4ebe1aa92d2eae2b6b143cac68fd1ac3c48be AS build
ARG TARGETARCH
RUN test "$TARGETARCH" = arm64
WORKDIR /build
COPY Cargo.toml Cargo.lock build.rs copperconfig.ron ./
COPY src/ src/
COPY tests/*.rs tests/
RUN --mount=type=cache,id=copper-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=copper-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=copper-target,target=/build/target \
    cargo test --locked && cargo build --locked --release \
    && install -Dm755 target/release/copper-rover /out/copper-rover

FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8
ARG IMAGE_REVISION=development
LABEL org.opencontainers.image.title="Copper × Admiral autonomy demo" \
      org.opencontainers.image.description="ARM64 Copper warehouse-rover simulation with native recording, deterministic replay, and mission-control UI" \
      org.opencontainers.image.revision="${IMAGE_REVISION}" \
      org.opencontainers.image.vendor="Admiral" \
      org.opencontainers.image.licenses="Apache-2.0" \
      co.admrl.workload="copper-autonomy"
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 tini util-linux ca-certificates libgcc-s1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 copper \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin copper \
    && install -d -o 10001 -g 10001 -m 0750 /data /data/runs
WORKDIR /opt/copper
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    COPPER_DATA_DIR=/data COPPER_STEPS=800 COPPER_KEEP_RUNS=4 COPPER_MAX_BYTES=134217728 \
    COPPER_VERSION=1.3.1 IMAGE_REVISION=${IMAGE_REVISION} PORT=8080
COPY --from=build /out/copper-rover /usr/local/bin/copper-rover
COPY service.py boot.py ./
COPY www/ www/
COPY tests/test_service.py tests/test_boot.py tests/check_image.py tests/
COPY scripts/start-copper /usr/local/bin/start-copper
COPY Cargo.toml Cargo.lock build.rs copperconfig.ron ./source/
COPY src/ ./source/src/
COPY README.md LICENSE NOTICE ./
RUN chmod 0755 /usr/local/bin/start-copper \
    && python3 -m unittest discover -s tests -v \
    && sha256sum service.py boot.py www/index.html www/app.js www/model.js www/style.css \
       source/Cargo.toml source/Cargo.lock source/build.rs source/copperconfig.ron source/src/*.rs \
       > packaged-sha256.txt
EXPOSE 8080/tcp
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/start-copper"]
