# Trimlight Edge — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/Michael-Civitillo/trimlight-edge-ha)](https://github.com/Michael-Civitillo/trimlight-edge-ha/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![hassfest](https://github.com/Michael-Civitillo/trimlight-edge-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/Michael-Civitillo/trimlight-edge-ha/actions/workflows/validate.yml)

Control your **Trimlight Edge** permanent LED holiday lights from Home Assistant.

Trimlight Edge lights are addressable RGB strips designed for permanent exterior installation. This integration connects via the official Trimlight cloud API, exposing each controller as a HA light entity with on/off, brightness, and effect selection.

---

## Features

- **On / Off** control for each Trimlight device
- **Brightness** adjustment (maps to the running effect's brightness)
- **Effect selection** — choose from any of the effects saved on the device (up to 60)
- **Multi-device** — all devices on your account appear automatically
- **Availability tracking** — entities go unavailable when the device is offline
- **30-second cloud polling** for state synchronisation

---

## Prerequisites

You need API credentials from Trimlight:

| Credential | How to get it |
|---|---|
| **Client ID** | Contact Trimlight support and request developer API access |
| **Client Secret** | Provided alongside the Client ID |

> Trimlight's documentation states: *"Please contact our business to obtain clientId and clientSecret."*
> Email their support team and mention you're building a Home Assistant integration.

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations**
3. Click the three-dot menu → **Custom repositories**
4. Add `https://github.com/Michael-Civitillo/trimlight-edge-ha` with category **Integration**
5. Search for **Trimlight Edge** and click **Download**
6. Restart Home Assistant

### Manual

1. Download the [latest release](https://github.com/Michael-Civitillo/trimlight-edge-ha/releases/latest)
2. Unzip and copy the `custom_components/trimlight` folder into your HA config directory:
   ```
   config/custom_components/trimlight/
   ```
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Trimlight Edge**
3. Enter your **Client ID** and **Client Secret**
4. Click **Submit** — your devices will appear automatically

---

## How It Works

The integration polls the Trimlight cloud API (`trimlight.ledhue.com`) every 30 seconds. Each request is authenticated with a per-request HMAC-SHA256 token derived from your credentials and the current timestamp.

On each poll it:
1. Notifies the device to report its latest state
2. Fetches the full device detail (switch state, current effect, saved effects list)

---

## Known Limitations

- **Effect state after app changes**: If you change the active effect from the Trimlight mobile app, HA will not reflect the new effect name until you select one through HA (the API's `currentEffect` does not include the effect name).
- **Brightness for custom effects**: Adjusting brightness in HA only works for **built-in effects** (the 180 factory presets). Custom effects require pixel data that isn't available from the API's `currentEffect` response.
- **Local control**: There is no local API — all communication goes through the Trimlight cloud. If the cloud is unavailable, devices will appear as unavailable in HA.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). All contributions welcome — especially:
- Testing against real hardware and reporting issues
- Adding support for more light features (color temp, RGB via custom effect preview)
- Writing tests

---

## License

MIT — see [LICENSE](LICENSE).
