# syntax=docker/dockerfile:1
# Railway service C — gosom/google-maps-scraper (MIT), run UNMODIFIED in web/REST mode.
# Pinned by tag AND digest (Docker Hub, 2026-08-22). v1.18.x left web jobs "working" after the scrape ended
# (no "ok" status, nothing to download): stay on v1.17.4 until upstream fixes it.
FROM gosom/google-maps-scraper:v1.17.4@sha256:b5cb22cfe7d91f81d66d090e72ac9b3806350ae74b74bcbf244e8a54edfa3495

# Job metadata (SQLite) and CSV results live here. Mount a Railway volume at /gmapsdata to keep jobs across
# restarts (optional: Scout deletes each job after downloading it and recreates jobs that vanished).
# No Dockerfile VOLUME instruction: Railway volumes are attached from the service settings instead.
ENV PORT=8080
EXPOSE 8080

# -web: REST API + UI on -addr; -c: concurrent scrape workers (keep low on a small instance).
ENTRYPOINT ["google-maps-scraper"]
CMD ["-web", "-addr", ":8080", "-data-folder", "/gmapsdata", "-c", "2"]
