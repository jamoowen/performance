FROM --platform=$TARGETPLATFORM rust:1.93.0-slim-bookworm AS build
WORKDIR /src
COPY benchmarks/http/ramp/rust/Cargo.toml benchmarks/http/ramp/rust/Cargo.lock* ./
RUN mkdir src && printf 'fn main() {}' > src/main.rs && cargo build --release --locked
COPY benchmarks/http/ramp/rust/ ./
ARG TARGETARCH
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN useradd --system --uid 65532 app
RUN mkdir /data && chown app:app /data
COPY --from=build /src/target/release/performance-http-ramp-rust /http-ramp-rust
USER app
ENV PORT=8080 SQLITE_PATH=/data/benchmark.sqlite FRAMEWORK=axum
EXPOSE 8080
ENTRYPOINT ["/http-ramp-rust"]
