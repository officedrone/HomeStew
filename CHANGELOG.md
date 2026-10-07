# Changelog

All notable changes to this project are documented in this file.

## [0.2.0]

- REFACTOR: The single `frontend/app.js` (5,580 lines) is split into 13 realm-based files under `frontend/js/` (`core/`, `devices/`, `calendar/`, `dashboard/`, `chat/`, `search/`, `settings/`, `wizard/` plus `bootstrap.js`). Loaded as classic scripts in dependency order with `bootstrap.js` last; behavior is unchanged. The monolithic `setupEventListeners()` is decomposed into per-realm `wireXxxEvents()` functions called from `wireAll()`. Guarded by `tests/test_frontend_split.py`.
- ADD: The icon picker now has live search across the full Lucide set (~2,100 icons); a blank search shows 40 common glyphs and "Load more" pages long results.
- ADD: The assistant can set any Lucide icon by name via chat and gained a `search_icons` action to find names first.
- UPDATE: Icons are stored as raw Lucide names; unknown legacy keys reset to the default plug icon on startup.
- FIX: Device name on a device card now stays left-aligned next to its icon at any card width (previously it floated toward center as the card grew).

## [0.1.6]

- ADD: "New Device" now offers an AI shortcut when a model is configured: a chooser asks whether to enter details manually or add the device via AI Chat.
- ADD: "None" option in Settings > Notifications > Webhook Type, now the default.
- ADD: The assistant can now set/change/reset a device's icon via chat (the `manage_devices` tool gained an `icon` parameter constrained to the same 40-key set the UI picker uses). Ask it things like "give my fridge a coffee-maker icon" or "reset the router's icon"; it updates live in the sidebar and Devices grid.
- ADD: Per-device icons powered by the Lucide icon set (vendored offline, ISC). Click a device's icon (card or sidebar) to pick from 40 appliance / electronics / BBQ & home glyphs; also selectable via an Icon row in the Add/Edit forms.
- UPDATE: Edit Device modal now shows field titles above every input (Device Name, Brand, Model, Description, Serial Number, Product Number), matching the Purchase Date style.

## [0.1.5]

- UPDATE: Change Settings > AI and Fist Start Wizard > AI model refresh behaviour
- UPDATE: For Settings pages, move setting/field descriptions to tool-tips instead of in page body.
- ADD: Fetch Manuals results window now allows users to change the search terms if needed

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
