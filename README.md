# Trimlight Edge — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/Michael-Civitillo/trimlight-edge-ha)](https://github.com/Michael-Civitillo/trimlight-edge-ha/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![hassfest](https://github.com/Michael-Civitillo/trimlight-edge-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/Michael-Civitillo/trimlight-edge-ha/actions/workflows/validate.yml)

I built this integration because I have Trimlight Edge lights on my house and wanted to control them from Home Assistant like everything else. No official HA integration existed, so I made one.

This connects to the Trimlight cloud API and exposes each of your controllers as a light entity — on/off, color picker, brightness, and switching between your saved effects. If you have Trimlight Edge lights and run Home Assistant, this is for you.

---

## What You Can Do

- Turn lights **on and off**
- **Color picker** — pick any color and it applies as a solid static effect
- Adjust **brightness**
- Switch between any **effects saved on the device** (up to 60)
- All your devices show up automatically — no manual configuration per device
- Lights show as unavailable in HA when the controller goes offline

---

## Before You Start — Getting API Credentials

This integration requires a **Client ID** and **Client Secret** from Trimlight. These aren't available publicly — you have to email Trimlight support and ask for developer API access. Mention you're building a Home Assistant integration.

> Trimlight's own documentation says: *"Please contact our business to obtain clientId and clientSecret."*

Once you have them, setup takes about 30 seconds.

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations**
2. Three-dot menu → **Custom repositories**
3. Add `https://github.com/Michael-Civitillo/trimlight-edge-ha`, category **Integration**
4. Search **Trimlight Edge** → **Download**
5. Restart Home Assistant

### Manual

1. Download the [latest release](https://github.com/Michael-Civitillo/trimlight-edge-ha/releases/latest)
2. Copy the `custom_components/trimlight` folder into your HA config:
   ```
   config/custom_components/trimlight/
   ```
3. Restart Home Assistant

---

## Setup

1. **Settings → Devices & Services → Add Integration**
2. Search **Trimlight Edge**
3. Enter your Client ID and Client Secret
4. Done — your devices appear automatically

---

## How It Works

Every 30 seconds, the integration polls the Trimlight cloud API (`trimlight.ledhue.com`). Before each poll it asks the device to report its latest state, then fetches the full detail — switch state, current effect, and your saved effects list.

Authentication uses the official HMAC-SHA256 token scheme from the Trimlight V2 API docs.

---

## Example Automations

This is where having your lights in Home Assistant actually pays off — you can tie them into everything else in your smart home.

**Flash red when the alarm is triggered**
```yaml
automation:
  - alias: "Flash lights red on alarm"
    trigger:
      - platform: state
        entity_id: alarm_control_panel.home
        to: "triggered"
    action:
      - service: light.turn_on
        target:
          entity_id: light.front_house
        data:
          effect: "Red Strobe"
          brightness: 255
```

**Switch to a holiday effect on a schedule**
```yaml
automation:
  - alias: "Christmas lights on at sunset"
    trigger:
      - platform: sun
        event: sunset
    condition:
      - condition: template
        value_template: "{{ now().month == 12 }}"
    action:
      - service: light.turn_on
        target:
          entity_id: light.front_house
        data:
          effect: "Christmas"
          brightness: 200
      - delay: "04:00:00"
      - service: light.turn_off
        target:
          entity_id: light.front_house
```

**Turn off when everyone leaves**
```yaml
automation:
  - alias: "Lights off when nobody home"
    trigger:
      - platform: state
        entity_id: zone.home
        to: "0"
    action:
      - service: light.turn_off
        target:
          entity_id: light.front_house
```

The point is — once your Trimlight is in HA, it plays nicely with everything else. Presence detection, alarm systems, calendar events, other smart home devices — all from one place.

---

## Known Limitations

- **Effect name after app changes** — if you switch effects in the Trimlight app, HA won't know the name of the new effect until you pick one through HA. The API doesn't include effect names in the running state.
- **Cloud only** — there's no local API. Everything goes through Trimlight's servers. If their cloud is down, your lights will show as unavailable in HA (but will still work from the app and their own timers).
- **Rate limiting** — the Trimlight API rejects rapid concurrent requests. The integration throttles calls with a 300ms minimum gap. Very fast color changes from the HA UI are smoothed out automatically.

---

## Disclaimer

This is a personal project, not affiliated with or endorsed by Trimlight. Use it at your own risk. I'm not responsible for anything that goes wrong with your lights, your Home Assistant setup, or anything else. It works great for me — but your mileage may vary.

---

## Contributing

Found a bug? Have a Trimlight and want to help test? PRs and issues are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE).
