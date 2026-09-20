ARG GO_VERSION=1.26.6
FROM golang:${GO_VERSION}-bookworm AS build
ARG SERVICE
WORKDIR /src
COPY packages/novel-platform packages/novel-platform
COPY services/ingest-api services/ingest-api
COPY services/reader-api services/reader-api
COPY services/scraper services/scraper
RUN cd services/${SERVICE} && CGO_ENABLED=0 go build -trimpath -o /app .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /app /app
ENTRYPOINT ["/app"]
