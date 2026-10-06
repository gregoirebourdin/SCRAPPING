# syntax=docker/dockerfile:1
# Build: static binary (no cgo). Both pinned libraries require Go >= 1.25.
FROM golang:1.25-bookworm AS build
WORKDIR /src
ENV CGO_ENABLED=0 GOOS=linux GOTOOLCHAIN=local
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN go build -trimpath -ldflags="-s -w" -o /out/email-verifier .

# Runtime: distroless static, non-root (uid 65532).
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/email-verifier /email-verifier
USER nonroot:nonroot
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/email-verifier"]
