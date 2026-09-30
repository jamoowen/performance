ARG BUN_VERSION=1.4.0
FROM oven/bun:${BUN_VERSION}-slim
WORKDIR /app
ENV NODE_ENV=production DB_PATH=/data/benchmark.sqlite
COPY --chown=bun:bun benchmarks/http/bun/package.json benchmarks/http/bun/server.js ./
RUN mkdir /data && chown bun:bun /data
USER bun
EXPOSE 8080
CMD ["bun", "run", "server.js"]
