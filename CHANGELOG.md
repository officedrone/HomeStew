# Changelog

All notable changes to this project are documented in this file.

## [0.1.3]

- NEW: Settings > Search panel with background re-index / rebuild jobs, a dismissible progress toast, and chunk-size / auto-on-upload options.
- UPDATE: Relaxed the default Docker `HEALTHCHECK` probe interval from 60s to 5 minutes, Still debating if this healthcheck is even worth it:P

## [0.1.2]

- FIX: Cut idle CPU usage by replacing the costly Python-based Docker `HEALTHCHECK` with a cheaper stdlib `http.client` check that validates the status code
