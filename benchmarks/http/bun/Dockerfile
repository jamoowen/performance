ARG BUN_VERSION=1.4.0
FROM oven/bun:${BUN_VERSION}-slim
WORKDIR /app
ENV NODE_ENV=production DB_PATH=/data/benchmark.sqlite
COPY --chown=bun:bun benchmarks/http/bun/package.json benchmarks/http/bun/bun.lock ./
RUN bun install --frozen-lockfile --production
COPY --chown=bun:bun benchmarks/http/bun/server.js benchmarks/http/bun/storage.js benchmarks/http/bun/launcher.js benchmarks/http/bun/diagnostics.js ./
RUN mkdir /data && chown bun:bun /data
USER bun
EXPOSE 8080
CMD ["bun", "run", "launcher.js"]
