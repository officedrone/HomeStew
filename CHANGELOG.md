# Changelog

All notable changes to this project are documented in this file.

## [0.1.4]

- UPDATE: The sidebar no longer grows with the window — it keeps the same fixed width (221px, what it used to be just above the mobile breakpoint) at every screen size. The mobile pop-up drawer matches that same width.
- UPDATE: Sidebar "Recent Devices" names now match the smaller font size of the "Upcoming" event titles, so both sidebar feeds read at the same size. Device cards on the Devices tab keep their larger name.
- FIX: Hard refresh the page after completing the first-run wizard, so freshly configured state (e.g. a master key created during setup) is reflected immediately instead of showing the stale pre-wizard view.

## [0.1.3]

- NEW: "Reasoning" checkbox in the first-run wizard and Settings > AI (checked by default).
- UPDATE: Settings > Search panel with background re-index / rebuild jobs, a dismissible progress toast, and chunk-size / auto-on-upload options.
- UPDATE: Relaxed the default Docker `HEALTHCHECK` probe interval from 60s to 5 minutes, Still debating if this healthcheck is even worth it:P

## [0.1.2]

- FIX: Cut idle CPU usage by replacing the costly Python-based Docker `HEALTHCHECK` with a cheaper stdlib `http.client` check that validates the status code
