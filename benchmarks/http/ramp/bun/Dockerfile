ARG BUN_VERSION=1.4.2
FROM oven/bun:${BUN_VERSION}-slim
WORKDIR /app
ENV NODE_ENV=production SQLITE_PATH=/data/benchmark.sqlite
COPY --chown=bun:bun benchmarks/http/ramp/bun/package.json benchmarks/http/ramp/bun/bun.lock ./
RUN bun install --frozen-lockfile --production
COPY --chown=bun:bun benchmarks/http/ramp/bun/server.js benchmarks/http/ramp/bun/storage.js ./
RUN mkdir /data && chown bun:bun /data
USER bun
EXPOSE 8080
CMD ["bun", "run", "server.js"]
