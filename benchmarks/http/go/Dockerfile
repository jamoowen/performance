ARG GO_VERSION=1.27.1
FROM golang:${GO_VERSION}-alpine AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src/benchmarks/http/go
RUN apk add --no-cache gcc musl-dev
COPY benchmarks/http/go/go.mod benchmarks/http/go/go.sum ./
RUN go mod download
COPY benchmarks/http/go/*.go ./
RUN mkdir -p /data && chown 65534:65534 /data
RUN CGO_ENABLED=1 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -tags sqlite_omit_load_extension -trimpath -ldflags="-s -w -linkmode external -extldflags '-static'" -o /out/http-go .

FROM scratch
COPY --from=build /etc/passwd /etc/passwd
COPY --from=build /out/http-go /http-go
COPY --from=build --chown=65534:65534 /data /data
ENV DB_PATH=/data/benchmark.sqlite
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["/http-go"]
