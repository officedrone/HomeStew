# Changelog

All notable changes to this project are documented in this file.

## [0.2.7]

- ADD: **See which search engines are working while manuals are fetched.** In the New/Edit device window, *Fetch Manuals* now reports the engine chain live: each configured engine appears in the dialog the moment it is queried (`Searching...` with a spinner) and updates as it answers — `N results`, `Answered, but no usable manuals`, `No results`, or `Error: <why>`. Previously the dialog showed one spinner until every engine had been tried, so a blocked engine was indistinguishable from a slow search. When the search finishes, the same panel becomes an expandable **Search engines** summary *below* the manuals table, headed with how many engines were tried and how many failed; engines that errored or answered with nothing usable start expanded so breakage is impossible to miss, and expanding one lists the manuals it found. The manuals themselves stay in one flat table with their Store buttons.
- ADD: A **Search Settings** button next to the Fetch Manuals search bar jumps straight to Settings > Search, where engines can be reordered, enabled/disabled and tested. Your device form, search results and progress are untouched by the trip — the dialogs are only hidden and are restored exactly as they were when you leave Settings.
- UPDATE: Manual searches stream over server-sent events (`GET /api/downloads/{id}/search-stream`); if a proxy or older server cannot stream, the dialog falls back to the plain JSON search endpoint automatically. Closing the dialog mid-search cancels the walk instead of letting a stale answer render into it.

- ADD: **Find a device by typing.** The Devices tab gains a filter box under *New Device* / *New Category*. Start typing and the grid narrows live, on every keystroke — no button, no waiting, nothing leaves your browser. It matches each word against the device's **name, brand and model**, so `lg` finds *Living Room TV* by its brand and `nespresso pixie` narrows to the coffee machine. Words combine (all must match), order doesn't matter, and case is ignored. A matching **category name** reveals that whole section with all of its devices — type `kitchen` to see the Kitchen group in full. A counter beside the box reports `N of 12`, an empty result says so plainly instead of leaving a blank page, and the *×* button (or Escape) puts everything back. Drag handles are hidden while a filter is active, because dropping a card into a list you can't fully see would park it in an invisible spot; clearing the filter brings them back.

## [0.2.6]

- ADD: **Take a photo, don't just upload one.** The Photos section gains a camera button next to *Upload Photo*: on a phone it opens the rear camera directly (the same `capture="environment"` trick AI Chat uses), on a desktop it falls back to a file picker. The shot follows the identical downscale-and-upload path, so labels, notes and *Analyze with AI now* all work the same.
- ADD: The **Add New Device** form now has its own **Photos** section with the same upload/camera pair and vision toggle. Pictures are downscaled and previewed in the form (each removable before you commit) and uploaded to the new device the moment you save it — reported in the success message alongside attributes and manuals.

## [0.2.5]

- ADD: **Device photos.** Every device can now hold pictures — a filter size, a nameplate, a breaker panel, the back of a remote. Open a device and use the new **Photos** section: upload one or more images (PNG/JPEG/WebP/GIF), give each a **label** and free-form **notes**, click a thumbnail for a full-screen lightbox, and delete what you no longer need. Images are downscaled in your browser before upload (max 2048px), so phone photos never bloat the server.
- ADD: **AI photo notes.** With **Model supports Vision** enabled, an uploaded photo can be analyzed right away (tick *Analyze with AI now*) or left for later. The AI's description is stored in its own **AI Notes** block, separate from your own notes, and never overwrites them silently: re-checks produce a *suggestion* you **Accept** or **Decline**.
- ADD: Chat can re-examine photos on demand. When the assistant needs to look at a device's pictures it offers an **attach photos** link in its reply — clicking it pulls that device's images into the chat input, ready to send. Photo text is never part of the top-level device list the model sees; the assistant fetches it only when you ask about pictures, so ordinary chats stay lean.
- ADD: Labels, your notes and the AI notes are all **searchable** from the Search tab alongside manual text.
- ADD: Settings > Notifications gains a **Photo AI suggestions pending** event, so an un-reviewed AI note resurfaces (once every 12 hours by default) until you accept or decline it.
- UPDATE: Backups are format version 2 and now carry device photos (image files + metadata, including pending AI suggestions). Restores keep working with older v1 archives; *Merge* skips photos that already exist, *Replace* rebuilds them exactly. The backup summary reports the photo count.
- FIX: Deleting a device left its uploaded files on disk. The whole device directory is now removed with the device.

## [0.2.4]

- UPDATE: Settings > Notifications is now fully per-event. The **"Enable notifications"** master checkbox and the separate "Check every (minutes)" / "Notify before due" fields are gone — the page is just the Webhook section and the Notification Events table.
- UPDATE: An event is enabled exactly when at least one of its **"Notify via"** channels is ticked; unticking all of them switches the event off. The redundant **Enable** column was removed from the Events table — there is no separate enable flag anywhere any more.
- ADD: The Events table gains editable per-row fields: **Interval (min)** now applies to calendar reminders too (their scan cadence), and a new **Notify before due (hrs)** column holds the calendar lead window (condition checks show **Not Applicable** — they have no due date). Both apply without a restart.
- ADD: A **Check Now** button on every row, including calendar reminders: it forces one immediate evaluation (a notifier scan for the calendar event) ignoring the interval, then refreshes the feed.
- FIX: The **Test** button now fires through the channel checkboxes exactly as shown on screen. Previously it only used the _saved_ subscription, so ticking "Webhook" and clicking Test before pressing Save produced just the local toast preview with no webhook send.
- UPDATE: The Events table is usable on small screens: it scrolls horizontally inside its frame (instead of crushing columns into unreadable slivers), with tighter padding and narrower fixed columns under 640px.

- ADD: Unified toast notification framework that events can use to display notifications to users . Every notification is now an **event** (what happened) delivered over the **channels** you subscribe to (in-app toast, webhook, future ones). Routing lives in one place (`services/dispatch.py`); adding an event or a channel needs no UI changes.
- ADD: Subscribe 'un-indexed manuals found' event to the new notification framework
- ADD: Settings > Notifications gains an **Events** table
- UPDATE: The notification ledger is now per-channel, so an in-app toast and a webhook send no longer suppress each other, and a partially failed delivery retries only the failed channel. Backups carry the channel column so restores keep both ledgers intact.
- UPDATE: All frontend toasts render through one notify API (`notifySuccess`/`notifyError`/`notifyInfo`/`notifyWarn`/`notifyEvent` in `core/utils.js`); a guard test rejects stray calls that bypass it.
- REFACTOR: Post-unification cleanup — removed the compatibility shims and load-time migrations left over from the notification rework (alias functions in `ui_notifications`/`notifier`, an unused subscriber probe in `dispatch`, the dead `UI_NOTIFICATIONS` settings migration), and made code comments version-agnostic. The ledger upgrade path for pre-channel webhook databases is kept.

## [0.2.3]

- SECURITY: Bumped `pypdf` 6.16.2 → 6.19.0 — fixes CVE-2026-102993, CVE-2026-102994, CVE-2026-102995, CVE-2026-102996, CVE-2026-102997, CVE-2026-102998, CVE-2026-102999, CVE-2026-103000 (8.7 High).
- SECURITY: Removed `pip` from the runtime Docker image entirely — fixes CVE-2025-8869, CVE-2026-13346, CVE-2026-6357, CVE-2026-3219, CVE-2026-8643, CVE-2026-1703 (pip) and its vendored tree: urllib3 CVE-2026-97689 / CVE-2026-97687 / CVE-2026-97688, setuptools CVE-2025-47273 / CVE-2026-59890, msgpack CVE-2026-57585 / GHSA-6v7p-g79w-8964. The app no longer installs packages at runtime, so pip was irrelevant.
- ADD: The first-run setup wizard now opens with a **"How would you like to start?"** step: **New instance** or **Restore from backup**. This allows the user to restore a backup to fresh instances.
- ADD: Restore Backup now runs with a proper status bar instead of hint text in the Settings body. A fixed bottom bar follows the whole pipeline. A second restore started while one runs is rejected with 409.
- ADD: Dedicated restore button in Settings > Backups

## [0.2.2]

- ADD: The assistant can now create and rename Devices-page categories via chat — `manage_devices` gained `create_category` and `rename_category` actions (deleting a section stays a UI action).
- UPDATE: Category section headers no longer show the tag glyph.
- ADD: Sections in Devices page; drag & drop support for sections.
- ADD: Drop animation — after a drag-and-drop the displaced device cards and category sections

## [0.2.1]

- ADD: Devices page cards are now collapsible.
- ADD: Add Support for OpenAI /Responses api endpoint
- ADD: "Provider" dropdown in Settings > AI and the first-run wizard: **Cloud - OpenAI** or **Custom**.

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
