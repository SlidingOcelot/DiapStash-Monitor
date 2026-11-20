# DiapStash — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

> **Vibe coded disclaimer:** This integration was built with AI pair programming assistance (Claude). It works, but has not been formally audited or extensively tested. Use at your own risk, contributions welcome.

A [Home Assistant](https://www.home-assistant.io/) integration that polls the [DiapStash](https://diapstash.com) Cloud Sync API and exposes sensors for the current diaper change and recent accidents.

## Entities

All entities belong to one **DiapStash** device.

### Sensors

| Entity | State | Notes |
|---|---|---|
| `sensor.diapstash_current_diaper` | Diaper type name(s) or `not wearing` | Type resolved from the DiapStash catalogue (public or custom types); stacked diapers comma-separated |
| `sensor.diapstash_diaper_state` | `Dry` / `Wet` / `Messy` / `Wet & Messy` | Derived from accidents during the active change; `unknown` when not wearing |
| `sensor.diapstash_change_started` | Timestamp | When the current diaper was put on |
| `sensor.diapstash_time_in_diaper` | Minutes | Time since the current diaper was put on; updates every minute |
| `sensor.diapstash_change_accidents` | Number | Accidents in the current change, with a timestamped list (see attributes) |
| `sensor.diapstash_wet_accident_count` | Number | Wetting accidents in the current change |
| `sensor.diapstash_messy_accident_count` | Number | Mess accidents in the current change |
| `sensor.diapstash_peak_wetness_level` | 0–5 | Highest wetting level in the current change |
| `sensor.diapstash_peak_messy_level` | 0–5 | Highest mess level in the current change |
| `sensor.diapstash_last_wetting` | Timestamp | Most recent wetting in the current change |
| `sensor.diapstash_last_mess` | Timestamp | Most recent mess in the current change |
| `sensor.diapstash_last_accident` | `wetting` or `mess` | Most recent accident overall (toilet visits excluded); all properties exposed as attributes |
| `sensor.diapstash_last_outside_accident` | `wetting` or `mess` | Most recent accident that happened while not wearing a diaper |
| `sensor.diapstash_accidents_today` | Number | Accidents since midnight (toilet visits counted separately) |
| `sensor.diapstash_accidents_last_7_days` | Number | Accidents in the last 7 days, with the last 20 accidents as history |
| `sensor.diapstash_api_requests_remaining` | Number | Diagnostic: API requests left in the current rate-limit hour |

### Binary sensors

| Entity | On when |
|---|---|
| `binary_sensor.diapstash_leak` | Any accident in the current change is marked as a leak |
| `binary_sensor.diapstash_messy_overflow` | The current change is flagged as a messy overflow |

Sensors tied to the current change are `unknown` while no diaper is being worn. Accidents logged with location `toilet` are ignored by the change-based sensors.

## Prerequisites

1. An active [DiapStash Cloud Sync](https://diapstash.com) account.
2. A registered **API client** in your DiapStash account settings (OAuth 2.0 client ID + secret).
   - Required scopes: `cloud-sync.history`, `cloud-sync.accidents`, `cloud-sync.types`
   - `cloud-sync.accidents` replaces the deprecated use of `cloud-sync.history` for accidents. Existing setups keep working for now; enable the scope on your API client so the next re-authorisation includes it.

## Installation via HACS

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add `https://github.com/SlidingOcelot/DiapStash-Monitor` with category **Integration**.
3. Search for *DiapStash* and click **Download**.
4. Restart Home Assistant.

## Manual Installation

Copy the `custom_components/diapstash` folder into your HA `config/custom_components/` directory and restart.

## Setup

1. In Home Assistant go to **Settings → Devices & Integrations → Credentials** and add your DiapStash OAuth2 client ID and client secret.
2. Go to **Settings → Devices & Integrations → Add Integration**, search for *DiapStash*, and follow the OAuth2 authorisation flow.

## Update interval

Sensors are refreshed every **5 minutes** by default. Change it under **Settings → Devices & Integrations → DiapStash → Configure** (2 to 60 minutes).

Each refresh makes 2 API requests. The DiapStash API allows 120 requests per hour per API client and user, and reports the remaining quota with every response. If the remaining requests would not last until the quota resets, updates are automatically spread out over the rest of the hour; after an HTTP 429 the integration waits for the reset. `sensor.diapstash_api_requests_remaining` shows the current quota.

Time-based sensors (time in diaper, accidents today) update every minute without extra API requests.

Only one DiapStash account can be configured per Home Assistant instance. Refresh tokens expire after 7 days without use; HA then shows a repair notice to re-authorise.

## Events

Every newly logged accident fires a `diapstash_accident` event (once, on the first update that sees it) and appears in the logbook. Accidents that already existed when the integration was first set up are not announced. Announced accidents are remembered across restarts: accidents logged while HA was down are announced after it starts, if they were logged within the last 60 minutes. Event data: `id`, `type`, `when`, `precision_time`, `level`, `cause`, `cause_leak`, `location`, `position`, `linked_change_id`, `during_change`.

```yaml
automation:
  - alias: Notify on DiapStash accident
    trigger:
      - platform: event
        event_type: diapstash_accident
    action:
      - action: notify.notify
        data:
          message: "{{ trigger.event.data.type }} (level {{ trigger.event.data.level }})"
```

## Attributes

### `sensor.diapstash_current_diaper`
| Attribute | Description |
|---|---|
| `change_id` | Internal change ID |
| `start_time` | When the current diaper was put on |
| `change_period` | `DAY` or `NIGHT` |
| `note` | User note on the change |
| `tags` | User tags on the change |
| `diaper_count` | Number of diapers in the change (stacking) |
| `diapers` | List of diapers with `name`, `brand`, `variant`, `size`, `type_id`, `image_url` |
| `image_url` | Picture of the first diaper, from the catalogue |
| `image_urls` | Pictures of all diapers in the change |

### `sensor.diapstash_diaper_state`
| Attribute | Description |
|---|---|
| `accident_count` | Number of accidents linked to the active change |

### `sensor.diapstash_change_accidents`
| Attribute | Description |
|---|---|
| `wetting` / `mess` | Accidents of each type in the current change |
| `toilet_visits` | Toilet visits since the change started (not counted in the state) |
| `first_accident` / `last_accident` | Timestamps of the first and last accident |
| `minutes_dry_before_first_accident` | Minutes from putting the diaper on to the first accident |
| `minutes_since_last_accident` | Minutes since the last accident |
| `accidents` | All accidents of the change, oldest first, with the fields listed for `last_accident` |

### `sensor.diapstash_accidents_today` / `sensor.diapstash_accidents_last_7_days`
| Attribute | Description |
|---|---|
| `wetting` / `mess` | Accidents of each type in the period |
| `toilet_visits` | Toilet visits in the period |
| `since` | Start of the period |
| `history` | 7-day sensor only: the last 20 accidents (newest first, toilet visits included) |

### `sensor.diapstash_last_accident`
| Attribute | Description |
|---|---|
| `id` | Accident UUID |
| `when` | Timestamp of the accident |
| `precision_time` | `exact` or `unknown` |
| `level` | Severity 0–5 |
| `cause` | `purpose`, `uncontrolledAwake`, `uncontrolledSleeping` |
| `cause_leak` | Whether a leak caused the accident |
| `location` | `diaper` or `self` (toilet visits are not accidents) |
| `position` | `standing`, `sitting`, `lying`, `squatting` |
| `linked_change_id` | Change the accident is linked to |
| `created_at` | Record creation timestamp |
| `updated_at` | Record last-updated timestamp |
| `last_toilet_visit` | Timestamp of the most recent toilet visit |
| `last_toilet_visit_type` | `wetting` or `mess` for that toilet visit |
