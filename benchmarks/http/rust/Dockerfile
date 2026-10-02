FROM rust:1.93.0-slim-bookworm AS build
WORKDIR /src/benchmarks/http/rust
COPY benchmarks/http/rust/Cargo.toml benchmarks/http/rust/Cargo.lock benchmarks/http/rust/build.rs ./
COPY benchmarks/http/rust/src ./src
RUN cargo fetch --locked
RUN cargo build --release --locked

FROM debian:12.12-slim
RUN mkdir -p /data && chown 65534:65534 /data
COPY --from=build /src/benchmarks/http/rust/target/release/performance-http-rust /usr/local/bin/performance-http-rust
USER 65534:65534
ENV DB_PATH=/data/benchmark.sqlite
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/performance-http-rust"]
