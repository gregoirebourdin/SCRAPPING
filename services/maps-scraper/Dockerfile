# syntax=docker/dockerfile:1
# Railway service C — gosom/google-maps-scraper (MIT), run UNMODIFIED in web/REST mode.
# Pinned by tag AND digest (Docker Hub, 2026-09-20): bump deliberately, re-run the adapter tests, then deploy.
FROM gosom/google-maps-scraper:v1.18.1@sha256:e205c02913c5a69c16fc2094b8e5b194a2f655166d2c1126d50548d216e6b1b2

# Job metadata (SQLite) and CSV results live here. Mount a Railway volume at /gmapsdata to keep jobs across
# restarts (optional: Scout deletes each job after downloading it and recreates jobs that vanished).
# No Dockerfile VOLUME instruction: Railway volumes are attached from the service settings instead.
ENV PORT=8080
EXPOSE 8080

# -web: REST API + UI on -addr; -c: concurrent scrape workers (keep low on a small instance).
ENTRYPOINT ["google-maps-scraper"]
CMD ["-web", "-addr", ":8080", "-data-folder", "/gmapsdata", "-c", "2"]
