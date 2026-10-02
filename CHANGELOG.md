# Changelog

All notable changes to this project are documented in this file.

## [0.1.5]

- UPDATE: Change Settings > AI and Fist Start Wizard > AI model refresh behaviour
- UPDATE: For Settings pages, move setting/field descriptions to tool-tips instead of in page body.

## [0.1.4]

- FIX: Inconsistent behaviour when using browser back/forward nav buttons
- ADD: Dashboard page
- UPDATE: Styling updates - Sidebar width, Recent devices/upcoming events font size, whitespace around the logo
- FIX: Hard refresh the page after completing the first-run wizard, so freshly configured state (e.g. a master key created during setup) is reflected immediately instead of showing the stale pre-wizard view.

## [0.1.3]

- NEW: "Reasoning" checkbox in the first-run wizard and Settings > AI (checked by default).
- UPDATE: Settings > Search panel with background re-index / rebuild jobs, a dismissible progress toast, and chunk-size / auto-on-upload options.
- UPDATE: Relaxed the default Docker `HEALTHCHECK` probe interval from 60s to 5 minutes, Still debating if this healthcheck is even worth it:P

## [0.1.2]

- FIX: Cut idle CPU usage by replacing the costly Python-based Docker `HEALTHCHECK` with a cheaper stdlib `http.client` check that validates the status code
