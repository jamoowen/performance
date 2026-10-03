ARG NODE_VERSION=24.21.0
FROM node:${NODE_VERSION}-bookworm-slim AS build
WORKDIR /app
COPY benchmarks/http/ramp/node/package.json benchmarks/http/ramp/node/package-lock.json ./
RUN npm ci
COPY benchmarks/http/ramp/node/tsconfig.json ./
COPY benchmarks/http/ramp/node/src ./src
RUN npm run build && npm prune --omit=dev

FROM node:${NODE_VERSION}-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production SQLITE_PATH=/data/benchmark.sqlite
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/dist ./dist
RUN mkdir /data && chown node:node /data
USER node
EXPOSE 8080
CMD ["node", "dist/main.js"]
