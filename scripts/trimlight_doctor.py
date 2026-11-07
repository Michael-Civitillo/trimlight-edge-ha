#!/usr/bin/env python3
"""Diagnose a real Trimlight Edge setup against what this integration assumes.

Standard library only, so it runs anywhere Python 3.11+ does, including inside
the Home Assistant container, with nothing to install.

Two modes:

  probe       Read-only. Talks to the Trimlight cloud with your API credentials
              and reports what your controllers actually return, flagging the
              payload shapes and saved-effect conditions that are known to break
              this integration. Sends no writes of any kind.

  scenario    Drives your Home Assistant over its REST API to reproduce specific
              bugs on real hardware. These change your lights, so each one needs
              --allow-writes. Original effect and brightness are restored at the
              end of every scenario.

Examples
--------
  # Read-only health check of every controller on the account
  python3 trimlight_doctor.py probe --client-id ID --client-secret SECRET

  # Same, taking credentials from the environment instead of the command line
  export TRIMLIGHT_CLIENT_ID=... TRIMLIGHT_CLIENT_SECRET=...
  python3 trimlight_doctor.py probe

  # List the scenarios, then run one against a real light
  python3 trimlight_doctor.py scenario --list
  python3 trimlight_doctor.py scenario brightness-white \
      --ha-url http://homeassistant.local:8123 --ha-token TOKEN \
      --entity light.front_house --allow-writes

Credentials are never printed. Apply for them at
https://trimlight.ledhue.com/trimlight/v1/api-credentials/apply
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

API_BASE_URL = "https://trimlight.ledhue.com/trimlight"
REQUEST_SPACING = 0.4  # The cloud answers 20000 to requests sent back to back.
HA_COLOR_EFFECT_NAME = "HA Color"
EFFECT_CATEGORY_BUILTIN = 0
EFFECT_CATEGORY_CUSTOM = 1

GREEN, YELLOW, RED, DIM, BOLD, RESET = (
    ("\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)

_findings: list[tuple[str, str]] = []


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET}   {msg}")


def warn(msg: str, detail: str = "") -> None:
    print(f"  {YELLOW}WARN{RESET} {msg}")
    if detail:
        print(f"       {DIM}{detail}{RESET}")
    _findings.append(("WARN", msg))


def bad(msg: str, detail: str = "") -> None:
    print(f"  {RED}FAIL{RESET} {msg}")
    if detail:
        print(f"       {DIM}{detail}{RESET}")
    _findings.append(("FAIL", msg))


def info(msg: str) -> None:
    print(f"  {DIM}-{RESET}    {msg}")


def head(msg: str) -> None:
    print(f"\n{BOLD}{msg}{RESET}")


# --------------------------------------------------------------------------- #
# Trimlight cloud client (same auth scheme as custom_components/trimlight/api.py)
# --------------------------------------------------------------------------- #


class TrimlightError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class TrimlightClient:
    def __init__(self, client_id: str, client_secret: str, timeout: int = 20) -> None:
        self._id = client_id
        self._secret = client_secret
        self._timeout = timeout
        self._last_request = 0.0

    def _headers(self) -> dict[str, str]:
        ts = int(time.time() * 1000)
        sig = hmac.new(
            self._secret.encode(),
            f"Trimlight|{self._id}|{ts}".encode(),
            hashlib.sha256,
        ).digest()
        return {
            "authorization": base64.b64encode(sig).decode(),
            "S-ClientId": self._id,
            "S-Timestamp": str(ts),
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        elapsed = time.monotonic() - self._last_request
        if elapsed < REQUEST_SPACING:
            time.sleep(REQUEST_SPACING - elapsed)
        req = urllib.request.Request(
            f"{API_BASE_URL}{path}",
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            raise TrimlightError(f"HTTP {err.code} from {path}") from err
        except urllib.error.URLError as err:
            raise TrimlightError(f"Could not reach {path}: {err.reason}") from err
        except json.JSONDecodeError as err:
            raise TrimlightError(f"Unparseable response from {path}") from err
        finally:
            self._last_request = time.monotonic()

        if not isinstance(body, dict):
            raise TrimlightError(f"Unexpected response from {path}: {body!r}")
        code = body.get("code")
        if code != 0:
            raise TrimlightError(
                f"API error {code}: {body.get('desc', 'unknown')}", code=code
            )
        return body.get("payload")

    @staticmethod
    def current_date() -> dict[str, int]:
        # Local time: this value sets the device clock and drives its timers.
        now = datetime.now().astimezone()
        return {
            "year": now.year - 2000,
            "month": now.month,
            "day": now.day,
            "weekday": now.isoweekday() % 7 + 1,
            "hours": now.hour,
            "minutes": now.minute,
            "seconds": now.second,
        }

    def devices(self) -> list[dict]:
        payload = self.request("GET", "/v1/oauth/resources/devices", {"page": 0})
        return (payload or {}).get("data") or []

    def device_detail(self, device_id: str) -> Any:
        return self.request(
            "POST",
            "/v1/oauth/resources/device/get",
            {"deviceId": device_id, "currentDate": self.current_date()},
        )


# --------------------------------------------------------------------------- #
# Probe: read-only checks
# --------------------------------------------------------------------------- #


def _odd_name(name: str) -> str | None:
    """Return why a saved-effect name is risky, or None if it looks fine."""
    if name != name.strip():
        return "has leading or trailing whitespace"
    if not name:
        return "is empty"
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        return "contains control characters"
    if not name.isascii():
        return "contains non-ASCII characters"
    return None


def probe_effects(effects_raw: Any) -> None:
    """Check the saved-effects list, the usual source of trouble."""
    head("Saved effects")
    if effects_raw is None:
        warn(
            "The device reported effects as null.",
            "Before 1.2.3 this stopped the light entity from being created at "
            "all. On 1.2.3 it reads as an empty list.",
        )
        return
    if not isinstance(effects_raw, list):
        bad(f"effects is {type(effects_raw).__name__}, expected a list.")
        return

    non_dict = [e for e in effects_raw if not isinstance(e, dict)]
    effects = [e for e in effects_raw if isinstance(e, dict)]
    info(f"{len(effects_raw)} entries returned")
    if non_dict:
        warn(
            f"{len(non_dict)} entries are not objects and are discarded.",
            f"Example: {non_dict[0]!r}",
        )

    if not effects:
        warn(
            "No usable saved effects.",
            "Picking a colour will create an 'HA Color' effect on the "
            "controller.",
        )
        return

    # Duplicate names are the documented trigger for cloud error 12006.
    names = [e.get("name") for e in effects]
    seen: dict[Any, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    dupes = {n: c for n, c in seen.items() if c > 1}
    if dupes:
        bad(
            f"Duplicate effect names: {', '.join(f'{n!r} x{c}' for n, c in dupes.items())}",
            "Duplicate names are the condition users traced to 'API error "
            "12006: Device data could not be parsed'. Rename or delete the "
            "extras in the Trimlight app.",
        )
    else:
        ok("No duplicate effect names.")

    odd = [(n, _odd_name(n)) for n in names if isinstance(n, str) and _odd_name(n)]
    if odd:
        warn(
            f"{len(odd)} effect name(s) look risky.",
            "; ".join(f"{n!r} {why}" for n, why in odd),
        )
    else:
        ok("No effect names with odd characters or stray whitespace.")

    unnamed = [e for e in effects if not e.get("name")]
    if unnamed:
        warn(
            f"{len(unnamed)} effect(s) have no name and are hidden from the "
            "Home Assistant effect list."
        )

    # Category values. The cloud accepts only 0 and 1 on save.
    cats = sorted({e.get("category") for e in effects}, key=repr)
    unexpected = [c for c in cats if c not in (EFFECT_CATEGORY_BUILTIN, EFFECT_CATEGORY_CUSTOM)]
    info(f"category values reported: {cats}")
    if unexpected:
        warn(
            f"Unexpected category value(s): {unexpected}",
            "The cloud rejects anything but 0 or 1 on save with error 12008. "
            "1.2.2 and later derive the category from the effect's shape "
            "instead of echoing this back, so this is informational.",
        )
    else:
        ok("All categories are 0 or 1.")

    # Shape ambiguity: the integration decides built-in vs custom from these.
    ambiguous = [
        e
        for e in effects
        if bool(e.get("pixels")) == (e.get("pixelLen") is not None)
    ]
    if ambiguous:
        warn(
            f"{len(ambiguous)} effect(s) have an ambiguous shape.",
            "They carry both pixels and pixelLen, or neither, so the category "
            "falls back to whatever the device reported. Names: "
            + ", ".join(repr(e.get("name")) for e in ambiguous[:5]),
        )
    else:
        ok("Every effect is unambiguously built-in or custom.")

    # The slot this integration writes for the colour picker.
    ha_slots = [e for e in effects if e.get("name") == HA_COLOR_EFFECT_NAME]
    if len(ha_slots) > 1:
        bad(
            f"{len(ha_slots)} effects named {HA_COLOR_EFFECT_NAME!r}.",
            "Delete all but one in the Trimlight app. Duplicates here are "
            "both a symptom and a cause of error 12006. Deleting all of them "
            "is safe: the next colour pick recreates one.",
        )
    elif ha_slots:
        slot = ha_slots[0]
        colour = (slot.get("pixels") or [{}])[0].get("color")
        shown = f"#{colour:06x}" if isinstance(colour, int) else repr(colour)
        ok(
            f"One {HA_COLOR_EFFECT_NAME!r} slot, id={slot.get('id')}, "
            f"brightness={slot.get('brightness')}, first pixel {shown}"
        )
        info(
            "Home Assistant never reads this colour back, so its colour "
            "picker can disagree with the strip until you set a colour from "
            "Home Assistant."
        )
    else:
        info(f"No {HA_COLOR_EFFECT_NAME!r} slot yet; one is created on first colour pick.")


def probe_current_effect(detail: dict) -> None:
    head("Running effect")
    current = detail.get("currentEffect")
    if not isinstance(current, dict):
        warn(
            f"currentEffect is {type(current).__name__}, so nothing reports "
            "what is running."
        )
        return
    info(f"currentEffect keys: {sorted(current)}")
    if isinstance(current.get("brightness"), int):
        ok(f"Reports brightness {current['brightness']}.")
    else:
        warn(
            "currentEffect carries no integer brightness, so Home Assistant "
            "cannot sync the brightness slider from the device."
        )
    if current.get("id") is not None:
        ok(
            f"Reports the running effect id ({current['id']}). Home Assistant "
            "could identify the running effect from this."
        )
    else:
        warn(
            "currentEffect carries no id.",
            "This is why a brightness-only change can replace your running "
            "pattern with solid white: with nothing identifying the running "
            "effect, the integration falls back to its colour slot. Run the "
            "'brightness-white' scenario to confirm on your hardware.",
        )


def probe_schedule(detail: dict) -> None:
    head("Timer schedule")
    found = False
    for source in ("daily", "calendar"):
        entries = detail.get(source)
        if not isinstance(entries, list) or not entries:
            continue
        found = True
        info(f"{source}: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                warn(f"{source}[{i}] is not an object: {entry!r}")
                continue
            start, end = entry.get("startTime"), entry.get("endTime")
            keys = sorted(set((start or {})) | set((end or {})))
            if keys and keys not in (["hours", "minutes"], ["hour", "minute"]):
                warn(f"{source}[{i}] uses unexpected time keys: {keys}")
            for label, obj in (("startTime", start), ("endTime", end)):
                if not isinstance(obj, dict):
                    continue
                h = obj.get("hours", obj.get("hour"))
                m = obj.get("minutes", obj.get("minute"))
                if h == 24 or m == 60:
                    warn(
                        f"{source}[{i}].{label} is {h}:{m:02d}"
                        if isinstance(m, int)
                        else f"{source}[{i}].{label} is {h}:{m}",
                        "Home Assistant rejects hours above 23 and treats the "
                        "window as unusable, falling back to reporting the "
                        "light on.",
                    )
            if isinstance(start, dict) and start == end:
                warn(
                    f"{source}[{i}] has a zero-length window.",
                    "Home Assistant cannot tell an all-day window from an off "
                    "one, so it reports the light on.",
                )
    if not found:
        info("No daily or calendar entries.")
    if detail.get("switchState") == 2 and not found:
        warn(
            "The controller is in timer mode but reports no schedule, so Home "
            "Assistant always reports this light as on."
        )


def cmd_probe(args: argparse.Namespace) -> int:
    client = TrimlightClient(args.client_id, args.client_secret)
    head("Account")
    try:
        devices = client.devices()
    except TrimlightError as err:
        bad(f"Could not list devices: {err}")
        if getattr(err, "code", None) == 10001:
            info("Code 10001 means the credentials or the host clock are wrong.")
        return 2
    ok(f"Authenticated. {len(devices)} device(s) on the account.")
    if len(devices) >= 20:
        warn(
            f"{len(devices)} devices returned on page 0.",
            "The integration only ever requests page 0, so devices beyond the "
            "first page would be invisible to Home Assistant.",
        )

    for device in devices:
        if not isinstance(device, dict) or "deviceId" not in device:
            bad(f"Malformed device entry, skipped: {device!r}")
            continue
        device_id = device["deviceId"]
        name = device.get("name") or device_id
        print(f"\n{BOLD}{'=' * 70}{RESET}")
        print(f"{BOLD}Device: {name}{RESET}  {DIM}({device_id}){RESET}")
        print(f"{BOLD}{'=' * 70}{RESET}")

        head("Connection")
        connectivity = device.get("connectivity")
        if connectivity == 1:
            ok("Online.")
        else:
            warn(
                f"Offline (connectivity={connectivity}).",
                "Home Assistant marks the light unavailable and skips the "
                "detail fetch, so the rest of this report will be thin.",
            )
        info(f"switchState={device.get('switchState')} (0 off, 1 manual, 2 timer)")
        info(f"firmware={device.get('fwVersionName')}")

        if connectivity != 1:
            continue

        head("Device detail")
        try:
            detail = client.device_detail(device_id)
        except TrimlightError as err:
            bad(f"Detail fetch failed: {err}")
            if getattr(err, "code", None) == 12006:
                info(
                    "Code 12006 means the cloud cannot parse what this "
                    "controller has stored. Home Assistant raises a repair "
                    "issue for it. Check the Trimlight app for duplicate or "
                    "oddly named saved effects."
                )
            continue
        if not isinstance(detail, dict):
            bad(f"Detail payload is {type(detail).__name__}, expected an object.")
            continue
        ok(f"Detail fetched. Top-level keys: {sorted(detail)}")
        if "effects" not in detail:
            warn(
                "The detail payload has no 'effects' key at all.",
                "1.2.3 treats this as 'loaded with no effects', which allows "
                "creating a colour slot. If this controller does have saved "
                "effects, say so when you report this.",
            )

        probe_effects(detail.get("effects"))
        probe_current_effect(detail)
        probe_schedule(detail)

    head("Summary")
    fails = sum(1 for level, _ in _findings if level == "FAIL")
    warns = sum(1 for level, _ in _findings if level == "WARN")
    if not _findings:
        print(f"  {GREEN}Nothing to flag.{RESET}")
    else:
        print(f"  {fails} failure(s), {warns} warning(s):")
        for level, msg in _findings:
            colour = RED if level == "FAIL" else YELLOW
            print(f"    {colour}{level}{RESET} {msg}")
    print(
        f"\n{DIM}Nothing was written to any controller by this probe.{RESET}"
    )
    return 1 if fails else 0


# --------------------------------------------------------------------------- #
# Scenarios: drive Home Assistant over its REST API
# --------------------------------------------------------------------------- #


class HomeAssistant:
    def __init__(self, url: str, token: str, timeout: int = 30) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _call(self, method: str, path: str, payload: Any = None) -> Any:
        req = urllib.request.Request(
            f"{self._url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Home Assistant returned HTTP {err.code}: {detail}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"Could not reach Home Assistant: {err.reason}") from err
        return json.loads(raw) if raw else None

    def state(self, entity_id: str) -> dict:
        return self._call("GET", f"/api/states/{entity_id}")

    def turn_on(self, entity_id: str, **kwargs: Any) -> None:
        self._call(
            "POST",
            "/api/services/light/turn_on",
            {"entity_id": entity_id, **kwargs},
        )

    def turn_off(self, entity_id: str) -> None:
        self._call("POST", "/api/services/light/turn_off", {"entity_id": entity_id})


def _snapshot(ha: HomeAssistant, entity: str) -> dict:
    s = ha.state(entity)
    a = s.get("attributes", {})
    return {
        "state": s.get("state"),
        "effect": a.get("effect"),
        "brightness": a.get("brightness"),
        "hs_color": a.get("hs_color"),
        "effect_list": a.get("effect_list") or [],
    }


def _show(label: str, snap: dict) -> None:
    info(
        f"{label}: state={snap['state']} effect={snap['effect']!r} "
        f"brightness={snap['brightness']} hs_color={snap['hs_color']}"
    )


def _restore(ha: HomeAssistant, entity: str, before: dict) -> None:
    head("Restoring")
    try:
        if before["state"] != "on":
            ha.turn_off(entity)
            info("Turned the light back off.")
            return
        if before["effect"]:
            kwargs: dict[str, Any] = {"effect": before["effect"]}
            if isinstance(before["brightness"], int):
                kwargs["brightness"] = before["brightness"]
            ha.turn_on(entity, **kwargs)
            info(f"Restored effect {before['effect']!r}.")
        elif before["hs_color"] and isinstance(before["brightness"], int):
            ha.turn_on(
                entity,
                hs_color=list(before["hs_color"]),
                brightness=before["brightness"],
            )
            info("Restored the previous colour and brightness.")
        else:
            info("Nothing specific to restore; the light was left on.")
    except RuntimeError as err:
        bad(f"Could not restore the previous state: {err}")
        info("Set your effect or colour again from the Home Assistant UI.")


def scenario_brightness_white(ha: HomeAssistant, entity: str, settle: float) -> None:
    """A brightness-only command with no known effect repaints solid white."""
    head("Scenario: brightness-only command with no known running effect")
    print(
        "  Reproduces: the brightness slider replacing your running pattern\n"
        "  with a solid white 'HA Color' effect. Expected to fire when Home\n"
        "  Assistant does not know which effect is running, which is the case\n"
        "  on a fresh install and after any restart while the light was off."
    )
    before = _snapshot(ha, entity)
    _show("before", before)
    if before["effect"]:
        warn(
            f"Home Assistant already knows the effect is {before['effect']!r}.",
            "The bug needs an unknown effect. Restart Home Assistant while "
            "this light is off, then run this scenario before touching the "
            "light from Home Assistant.",
        )
        return
    if before["state"] != "on":
        warn("The light is off; turn it on at the controller first.")
        return

    target = 128 if before["brightness"] != 128 else 100
    info(f"Calling light.turn_on with brightness={target} and nothing else.")
    ha.turn_on(entity, brightness=target)
    time.sleep(settle)
    after = _snapshot(ha, entity)
    _show("after", after)

    if after["effect"] == HA_COLOR_EFFECT_NAME:
        bad(
            f"Reproduced. The effect became {HA_COLOR_EFFECT_NAME!r}.",
            "Your running pattern was replaced by a solid colour, and an "
            "extra effect may now be saved on the controller. Check the "
            "Trimlight app, and re-run the probe to look for duplicates.",
        )
    else:
        ok(f"Not reproduced. The effect is {after['effect']!r}.")
    _restore(ha, entity, before)


def scenario_brightness_roundtrip(ha: HomeAssistant, entity: str, settle: float) -> None:
    """Brightness returned to its earlier value inside one poll interval."""
    head("Scenario: brightness returned to an earlier value quickly")
    print(
        "  Reproduces the bug 1.2.3 fixes: setting brightness to a value, then\n"
        "  away, then back again inside one 30 second poll used to leave the\n"
        "  controller at the middle value while Home Assistant showed the last\n"
        "  one. On 1.2.3 the final value should stick."
    )
    before = _snapshot(ha, entity)
    _show("before", before)
    if before["state"] != "on":
        warn("The light is off; turn it on first.")
        return

    start = before["brightness"] if isinstance(before["brightness"], int) else 255
    middle = 60 if start > 120 else 220
    for value in (start, middle, start):
        info(f"brightness={value}")
        ha.turn_on(entity, brightness=value)
        time.sleep(2)

    info(f"Waiting {settle:.0f}s, then checking what Home Assistant reports.")
    time.sleep(settle)
    after = _snapshot(ha, entity)
    _show("after", after)
    if after["brightness"] == start:
        ok(f"Home Assistant reports the final value ({start}).")
        info(
            "Confirm by eye that the strip matches. Home Assistant holds "
            "optimistic state for 60s, so check again after a minute."
        )
    else:
        bad(
            f"Home Assistant reports {after['brightness']}, expected {start}."
        )
    _restore(ha, entity, before)


def scenario_effect_brightness(ha: HomeAssistant, entity: str, settle: float) -> None:
    """Selecting an effect leaves the brightness reading stale."""
    head("Scenario: brightness reading after selecting an effect")
    print(
        "  Reproduces: selecting a saved effect runs it at that effect's own\n"
        "  stored brightness, while Home Assistant keeps showing the previous\n"
        "  number for up to about 90 seconds."
    )
    before = _snapshot(ha, entity)
    _show("before", before)
    if before["state"] != "on":
        warn("The light is off; turn it on first.")
        return
    choices = [e for e in before["effect_list"] if e != before["effect"]]
    if not choices:
        warn("Need at least two saved effects to run this scenario.")
        return

    pick = choices[0]
    baseline = before["brightness"]
    info(f"Selecting effect {pick!r} with no brightness, from brightness {baseline}.")
    ha.turn_on(entity, effect=pick)
    time.sleep(settle)
    after = _snapshot(ha, entity)
    _show("after", after)

    if after["brightness"] == baseline:
        warn(
            f"Home Assistant still reports brightness {baseline} after "
            f"switching to {pick!r}.",
            "If the strip visibly changed brightness, this is the stale "
            "reading. Anything reading this entity gets the wrong value until "
            "the optimistic window expires.",
        )
    else:
        ok(f"Brightness updated to {after['brightness']}.")
    _restore(ha, entity, before)


SCENARIOS = {
    "brightness-white": (
        scenario_brightness_white,
        "Brightness-only command replaces the running effect with solid white",
    ),
    "brightness-roundtrip": (
        scenario_brightness_roundtrip,
        "Brightness set back to an earlier value inside one poll interval",
    ),
    "effect-brightness": (
        scenario_effect_brightness,
        "Brightness reading goes stale after selecting an effect",
    ),
}


def cmd_scenario(args: argparse.Namespace) -> int:
    if args.list or not args.name:
        print("Available scenarios:\n")
        for key, (_, desc) in SCENARIOS.items():
            print(f"  {BOLD}{key}{RESET}\n      {desc}")
        print(
            "\nEach one changes your lights, so pass --allow-writes to run it."
        )
        return 0
    if args.name not in SCENARIOS:
        print(f"Unknown scenario {args.name!r}. Use --list to see them.", file=sys.stderr)
        return 2
    missing = [
        flag
        for flag, value in (
            ("--ha-url", args.ha_url),
            ("--ha-token", args.ha_token),
            ("--entity", args.entity),
        )
        if not value
    ]
    if missing:
        print(f"Missing required option(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    if not args.allow_writes:
        print(
            "This scenario changes your lights. Re-run it with --allow-writes "
            "once you are ready.",
            file=sys.stderr,
        )
        return 2

    ha = HomeAssistant(args.ha_url, args.ha_token)
    try:
        state = ha.state(args.entity)
    except RuntimeError as err:
        bad(str(err))
        return 2
    if not state or state.get("state") in (None, "unavailable", "unknown"):
        bad(f"{args.entity} is {state.get('state') if state else 'missing'}.")
        return 2

    func, desc = SCENARIOS[args.name]
    print(f"{BOLD}{args.name}{RESET}: {desc}")
    print(f"{DIM}Entity {args.entity}. The previous state is restored at the end.{RESET}")
    try:
        func(ha, args.entity, args.settle)
    except RuntimeError as err:
        bad(str(err))
        return 2

    head("Summary")
    fails = sum(1 for level, _ in _findings if level == "FAIL")
    for level, msg in _findings:
        colour = RED if level == "FAIL" else YELLOW
        print(f"  {colour}{level}{RESET} {msg}")
    if not _findings:
        print(f"  {GREEN}Nothing to flag.{RESET}")
    return 1 if fails else 0


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trimlight_doctor.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser(
        "probe", help="Read-only health check of the controllers on your account"
    )
    probe.add_argument(
        "--client-id",
        default=os.environ.get("TRIMLIGHT_CLIENT_ID"),
        help="defaults to $TRIMLIGHT_CLIENT_ID",
    )
    probe.add_argument(
        "--client-secret",
        default=os.environ.get("TRIMLIGHT_CLIENT_SECRET"),
        help="defaults to $TRIMLIGHT_CLIENT_SECRET",
    )
    probe.set_defaults(func=cmd_probe)

    scen = sub.add_parser(
        "scenario", help="Reproduce a specific bug against a real light"
    )
    scen.add_argument("name", nargs="?", help="scenario to run; omit to list them")
    scen.add_argument("--list", action="store_true", help="list the scenarios and exit")
    scen.add_argument(
        "--ha-url",
        default=os.environ.get("HA_URL", "http://homeassistant.local:8123"),
        help="base URL of Home Assistant; defaults to $HA_URL",
    )
    scen.add_argument(
        "--ha-token",
        default=os.environ.get("HA_TOKEN"),
        help="long-lived access token; defaults to $HA_TOKEN",
    )
    scen.add_argument("--entity", help="the light entity, e.g. light.front_house")
    scen.add_argument(
        "--allow-writes",
        action="store_true",
        help="required: confirms the scenario may change your lights",
    )
    scen.add_argument(
        "--settle",
        type=float,
        default=8.0,
        help="seconds to wait for state to settle (default 8)",
    )
    scen.set_defaults(func=cmd_scenario)

    args = parser.parse_args(argv)

    if args.command == "probe" and not (args.client_id and args.client_secret):
        parser.error(
            "probe needs --client-id and --client-secret, or "
            "$TRIMLIGHT_CLIENT_ID and $TRIMLIGHT_CLIENT_SECRET"
        )
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
