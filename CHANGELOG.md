# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Changing brightness no longer replaces the running effect with a solid color — the running effect is re-saved with the new brightness instead. Brightness combined with an effect in one call now applies to that effect.
- Failed on/off/effect/color commands now surface as errors in Home Assistant instead of reporting success while the lights stay unchanged, and the optimistic state is rolled back so HA doesn't show a wrong state for a minute afterwards. Selecting an effect that doesn't exist on the device fails the service call instead of being logged and ignored.
- Lights now show as unavailable when the Trimlight cloud can't be reached (as the README always promised) instead of staying "available" with frozen state.
- Weekday/weekend timer schedules no longer report the light as on for the whole non-scheduled day (e.g. all Saturday for a weekdays-only schedule); the same applies to calendar events outside their date range.
- Rapid color changes no longer race to create duplicate "HA Color" effects on the controller: each light's commands are serialized, a failed activation no longer discards the known-good color slot, and a timed-out effect *create* is not retried (the retry could land twice).
- The effect, color, and brightness shown in HA survive a restart, and brightness changes made in the Trimlight app now flow back into HA.
- Devices added to the Trimlight account appear in HA on the next poll — no reload needed.
- Rejected API credentials now trigger Home Assistant's re-authentication flow instead of failing silently forever.
- Malformed API payloads (a device without an id, an empty detail response) no longer fail the whole update cycle, and schedule times with out-of-range values are rejected instead of silently wrapped into a wrong window.
- An effect id of 0 from the API is no longer treated as invalid.

### Changed
- The Client Secret field in the setup form is now a password input.
- Polling skips the detail fetch for offline devices, and the best-effort shadow notify is no longer retried, keeping the poll cycle short when devices or the cloud misbehave.
- Now requires Home Assistant 2024.8 or newer.

### Internal
- The coordinator lives on `entry.runtime_data` and receives the config entry; the light platform declares `PARALLEL_UPDATES`.
- Test dependencies are pinned for reproducible CI; added a ruff configuration and CI lint job; workflows run with least-privilege permissions; releases are gated on the test suite and their notes contain only the released version's changelog section.

## [1.1.6] - 2026-06-27

### Fixed
- Retry backoff no longer holds the API lock: a failing cloud request used to block every other request (including a user pressing on/off) behind its full retry budget. The backoff now waits outside the lock, so commands stay responsive during a transient outage.
- Timer schedules that wrap past midnight are now read against the correct day. A weekday/weekend window like 22:00 → 02:00 kept its after-midnight hours attributed to the wrong day, which could drop the window and misreport the light's state.
- Disabled calendar events are now ignored when deriving timer state, matching how disabled daily schedules are already skipped. A disabled calendar event covering the current time no longer reports the light as on.
- While a device's detail fetch is failing, the carried-forward schedule is flagged stale and timer mode falls back to "on" instead of deriving on/off from a schedule that may no longer match the device.

### Internal
- Unified the daily and calendar schedule-evaluation paths into a single window-voting helper to remove the duplicated loops.

## [1.1.5] - 2026-06-26

### Fixed
- Transient cloud errors (502/504, timeouts, dropped connections) are now retried with backoff instead of failing the whole update, so a brief blip from `trimlight.ledhue.com` no longer flips the integration to "unavailable" ([#12])
- A device that's temporarily offline no longer loses its effect list and timer schedule in Home Assistant — the last-known detail is kept while only the live fields (connectivity, switch state) refresh, so the entity restores cleanly when the device comes back ([#12])
- The "Could not fetch detail for device" warning is now logged once per outage instead of on every 30-second poll, so an offline device no longer floods the log ([#12])

### Thanks
- [@jeffnewbold](https://github.com/jeffnewbold) for reporting the offline/error-fetching-data behaviour ([#12])

[#12]: https://github.com/Michael-Civitillo/trimlight-edge-ha/issues/12

## [1.1.4] - 2026-06-26

### Fixed
- Lights on a timer schedule now show as off in Home Assistant once the schedule turns them off — the device keeps reporting timer mode (`switchState=2`) whether the lights are currently lit or not, so the running state is now derived from the schedule's on/off windows instead of treating timer mode as always on ([#10])

### Thanks
- [@CptSugarFree](https://github.com/CptSugarFree) for reporting that timer-mode lights showed as on after the schedule turned them off ([#10])

[#10]: https://github.com/Michael-Civitillo/trimlight-edge-ha/issues/10

## [1.1.3] - 2026-06-23

### Fixed
- Effect dropdown no longer lags one selection behind — the active effect is now published to Home Assistant as soon as the command completes instead of on the next poll ([#7])
- Lights no longer flip to "off" in HA a minute or two after being turned on — turning on now sets the device's switch state to manual so the cloud shadow reports the device as on (previously `view_effect`/`save_effect` left the persisted switch state untouched) ([#7])
- Plain on/off toggle now lets the device resume the effect it was last showing instead of forcing the first saved effect or a stale "HA Color" slot, which could leave the strip static white ([#7])
- Color-to-color changes now repaint the physical lights — re-saving the "HA Color" slot updates the stored values but only `view_effect` makes the controller repaint, so a second solid color (e.g. red → blue) was silently ignored on the device while HA showed the new color ([#8])

### Thanks
- [@CptSugarFree](https://github.com/CptSugarFree) for reporting the effect-sync and "device shows off" issues ([#7])
- [@trevornorcross](https://github.com/trevornorcross) for reporting and precisely diagnosing the solid-color repaint bug ([#8])

[#7]: https://github.com/Michael-Civitillo/trimlight-edge-ha/issues/7
[#8]: https://github.com/Michael-Civitillo/trimlight-edge-ha/issues/8

## [1.1.2] - 2026-06-12

### Fixed
- Wrap network and HTTP errors in `TrimlightApiError` so failures surface cleanly instead of leaking raw exceptions
- Use the HA-configured timezone when sending the device's current date
- Recover automatically from a stale cached color effect id
- Drop a redundant device fetch during setup
- Harden effect list parsing and device payload handling against unexpected responses

### Changed
- README now documents how to obtain the API client secret

## [1.1.1] - 2026-05-19

### Added
- Brand icon and logo bundled with the integration for HA 2026.3+ local brand support — the Trimlight Edge logo now shows in the "Add Integration" picker and across the HA frontend

## [1.1.0] - 2026-03-28

### Added
- **Color picker** — pick any color from HA's color wheel and it applies as a solid static effect on the device
- **Brightness control** — brightness slider works with color picker and saved effects
- Rapid color change optimization — skips redundant API calls when the color slot is already active
- API request rate limiting (300ms minimum gap) to prevent server error 20000
- Connectivity check on startup (`ConfigEntryNotReady`) for graceful retry on boot
- Named constants for all API values (no more magic numbers)

### Changed
- Replaced deprecated `async_timeout` with Python 3.11+ `asyncio.timeout`
- Removed unused `preview_effect` API method (broken on firmware 1.17.4171)
- Improved error handling with `_LOGGER.exception()` for full stack traces
- Cleaner coordinator logging (merged data keys instead of full dumps)
- Uses `Platform.LIGHT` constant instead of string literal
- Added `integration_type`, `issue_tracker` to manifest for HACS compliance

### Fixed
- Color picker not working due to wrong effect category (was 1, device uses 2)
- Brightness changes resetting the active color
- Colors stopping after ~3 changes due to API rate limiting
- `set_switch_state(MANUAL)` clearing active effects after `view_effect`
- Pixel count exceeding API maximum in solid color effects

## [1.0.0] - 2025-03-22

### Added
- Initial release
- Cloud polling via `trimlight.ledhue.com` every 30 seconds
- Light entities with on/off, brightness, and effect selection
- Support for multiple devices per Trimlight account
- Availability tracking (online/offline)
- Config flow UI for entering Client ID and Client Secret
- HACS compatibility
