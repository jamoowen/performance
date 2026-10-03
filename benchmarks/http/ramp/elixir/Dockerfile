FROM hexpm/elixir:1.20.4-erlang-28.5.0.7-debian-bookworm-20260918-slim AS build

WORKDIR /app
ENV MIX_ENV=prod
RUN mix local.hex --force && mix local.rebar --force
COPY benchmarks/http/ramp/elixir/mix.exs benchmarks/http/ramp/elixir/mix.lock ./
RUN mix deps.get --only prod && mix deps.compile
COPY benchmarks/http/ramp/elixir/config ./config
COPY benchmarks/http/ramp/elixir/lib ./lib
RUN mix compile && mix release

FROM debian:bookworm-20260918-slim AS runtime
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgcc-s1 libncurses6 libssl3 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 benchmark \
    && mkdir /data \
    && chown benchmark:benchmark /data
WORKDIR /app
COPY --from=build /app/_build/prod/rel/ramp ./ramp
USER benchmark
ENV HOME=/app \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    RELEASE_DISTRIBUTION=none \
    RELEASE_TMP=/tmp/ramp \
    ERL_FLAGS="+S 1:1 +SDcpu 1 +SDio 1" \
    FRAMEWORK=phoenix \
    PORT=8080
EXPOSE 8080
CMD ["/app/ramp/bin/ramp", "start"]
