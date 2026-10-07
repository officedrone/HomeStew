// HomeStew - Cross-realm mutable state. Declared once here; every realm
// reads and writes these at runtime (classic scripts share global lexical scope).


let devices = [];
let currentDeviceFilter = null;
// Calendar tab state: device filter, date-range filter and loaded events.
// The range is a day horizon ('all', 7, 31, 182 or 365) sent as within_days.
let currentCalendarDeviceFilter = null;
let currentCalendarRange = 'all';
let calendarEvents = [];
// Selected device filter for the AI Chat tab (null = all devices). Mirrors
// currentDeviceFilter, but scoped to chat so the two tabs stay independent.
let currentChatDeviceFilter = null;
// Device id currently targeted by the hidden manual-upload input.
let uploadTargetDeviceId = null;

// Result of the last chat model availability check (see checkChatModelStatus).
// null = unknown/not yet checked; true/false = whether chatting is allowed.
let chatModelAvailable = null;

// Whether the saved config marks the model as vision-capable ("Model supports
// Vision" in Settings > AI / the first-run wizard). The attach & camera
// buttons are hidden while false, and sendChatMessage refuses images so an
// old tab can't push a photo to a text-only model. Seeded from
// /api/settings/model-status (see checkChatModelStatus) and refreshed after a
// settings save; the server independently rejects images too (chat API).
let chatVisionEnabled = false;

// Section requested by a deep link before loadSettingsPage() runs. That
// function ends by resetting to General, so the pending name is applied (and
// cleared) there instead of being overwritten after its async fetch.
let pendingSettingsSection = null;
