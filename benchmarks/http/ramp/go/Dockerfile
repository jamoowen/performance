FROM --platform=$BUILDPLATFORM golang:1.27.1-alpine AS build
WORKDIR /src
COPY benchmarks/http/ramp/go/go.mod benchmarks/http/ramp/go/go.sum ./
RUN go mod download
COPY benchmarks/http/ramp/go/ ./
ARG TARGETOS TARGETARCH
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags='-s -w' -o /out/http-ramp-go . && mkdir /out/data

FROM gcr.io/distroless/static-debian13:nonroot
COPY --from=build /out/http-ramp-go /http-ramp-go
COPY --from=build --chown=nonroot:nonroot /out/data /data
USER nonroot:nonroot
ENV PORT=8080 SQLITE_PATH=/data/benchmark.sqlite FRAMEWORK=nethttp
EXPOSE 8080
ENTRYPOINT ["/http-ramp-go"]
