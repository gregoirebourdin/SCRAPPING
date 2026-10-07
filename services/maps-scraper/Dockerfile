# syntax=docker/dockerfile:1
# Railway service C — gosom/google-maps-scraper (MIT) driven by mapsd, our job supervisor (supervisor/).
#
# gosom's own web runner stops dispatching after a few dozen jobs (they stay "pending" forever), so mapsd keeps
# the same REST API but runs each job as a fresh CLI process, killed with its browsers when it overruns.
# The scraper image is pinned by tag AND digest (Docker Hub, 2026-08-22); v1.17.4 is the last version verified
# end to end on Railway (27 places in 30 s). Bump deliberately, re-run `go test ./...` in supervisor/, deploy.

FROM golang:1.24-alpine AS build
WORKDIR /src
COPY supervisor/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/mapsd .

FROM gosom/google-maps-scraper:v1.17.4@sha256:b5cb22cfe7d91f81d66d090e72ac9b3806350ae74b74bcbf244e8a54edfa3495
COPY --from=build /out/mapsd /usr/local/bin/mapsd
# WORKERS jobs at a time, each with SCRAPER_CONCURRENCY browser pages (≈ 0.7 GB per worker).
ENV PORT=8080 WORKERS=2 SCRAPER_CONCURRENCY=2 DATA_DIR=/tmp/mapsd
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/mapsd"]
CMD []
