ARG K6_VERSION=1.3.0
FROM grafana/k6:${K6_VERSION}
COPY benchmarks/http/load.js /scripts/load.js
ENTRYPOINT ["k6", "run", "/scripts/load.js"]
