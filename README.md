# DiapStash — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

> **Vibe coded disclaimer:** This integration was built with AI pair programming assistance (Claude). It works, but has not been formally audited or extensively tested. Use at your own risk, contributions welcome.

A [Home Assistant](https://www.home-assistant.io/) integration that polls the [DiapStash](https://diapstash.com) Cloud Sync API and exposes three sensors.

## Sensors

| Entity | State | Notes |
|---|---|---|
| `sensor.diapstash_current_diaper` | Diaper type name(s) or `not wearing` | Type resolved from the DiapStash catalogue; multiple diapers comma-separated |
| `sensor.diapstash_diaper_state` | `Dry` / `Wet` / `Messy` / `Wet & Messy` | Derived from accidents linked to the active change; `unavailable` when not wearing |
| `sensor.diapstash_last_accident` | `wetting` or `mess` | All accident properties exposed as attributes |

## Prerequisites

1. An active [DiapStash Cloud Sync](https://diapstash.com) account.
2. A registered **API client** in your DiapStash account settings (OAuth 2.0 client ID + secret).
   - Required scopes: `cloud-sync.history`, `cloud-sync.types`

## Installation via HACS

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add `https://github.com/kribi/ha-diapstash` with category **Integration**.
3. Search for *DiapStash* and click **Download**.
4. Restart Home Assistant.

## Manual Installation

Copy the `custom_components/diapstash` folder into your HA `config/custom_components/` directory and restart.

## Setup

1. In Home Assistant go to **Settings → Devices & Integrations → Credentials** and add your DiapStash OAuth2 client ID and client secret.
2. Go to **Settings → Devices & Integrations → Add Integration**, search for *DiapStash*, and follow the OAuth2 authorisation flow.

## Update interval

Sensors are refreshed every **5 minutes** to be respectful of the DiapStash API.

## Attributes

### `sensor.diapstash_current_diaper`
| Attribute | Description |
|---|---|
| `change_id` | Internal change ID |
| `start_time` | When the current diaper was put on |
| `change_period` | `DAY` or `NIGHT` |
| `note` | User note on the change |
| `tags` | User tags on the change |

### `sensor.diapstash_diaper_state`
| Attribute | Description |
|---|---|
| `accident_count` | Number of accidents linked to the active change |

### `sensor.diapstash_last_accident`
| Attribute | Description |
|---|---|
| `id` | Accident UUID |
| `when` | Timestamp of the accident |
| `precision_time` | `exact` or `unknown` |
| `level` | Severity 0–5 |
| `cause` | `purpose`, `uncontrolledAwake`, `uncontrolledSleeping` |
| `cause_leak` | Whether a leak caused the accident |
| `location` | `diaper`, `toilet`, `self` |
| `position` | `standing`, `sitting`, `lying`, `squatting` |
| `linked_change_id` | Change the accident is linked to |
| `created_at` | Record creation timestamp |
| `updated_at` | Record last-updated timestamp |
