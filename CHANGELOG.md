# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
