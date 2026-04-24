# Dashboard & Automation Examples

Ready-to-use examples for monitoring your cable modem in Home Assistant.

## Table of Contents

- [Dashboard Generator Service](#dashboard-generator-service)
- [Manual Dashboard Example](#manual-dashboard-example)
- [Last Boot Time Display Options](#last-boot-time-display-options)
- [Automation Examples](#automation-examples)

---

## Dashboard Generator Service

The easiest way to create a dashboard is the built-in generator service.
It reads your modem's actual channel data and produces ready-to-paste
Lovelace YAML — no manual entity counting required.

### How to use

1. Open **Developer Tools > Actions** in Home Assistant
2. Select **Cable Modem Monitor: Generate Dashboard**
3. Toggle the sections you want (all enabled by default)
4. Click **Perform action**
5. Copy the YAML from the response
6. Go to your dashboard, click **Add Card > Manual**, paste the YAML

### Options

| Option | Default | Description |
|--------|---------|-------------|
| Status Card | on | Modem status, uptime, channel counts, restart button |
| Downstream Power | on | Power levels for all downstream channels |
| Downstream SNR | on | Signal-to-noise ratio for all downstream channels |
| Downstream Frequency | on | Frequency for all downstream channels |
| Upstream Power | on | Power levels for all upstream channels |
| Upstream Frequency | off | Frequency for all upstream channels |
| Error Graphs | on | Corrected and uncorrected error counts (7-day view) |
| Latency | on | Ping and HTTP latency (6-hour view) |
| Graph Hours | 24 | Hours of history shown in channel graphs (1-168) |
| Short Titles | off | Compact card titles (e.g., "DS Power" vs "Downstream Power Levels") |

The generated YAML is tailored to your modem — correct channel count,
channel types (QAM, OFDM, ATDMA, OFDMA), and entity prefix.

### Calling from an automation or script

```yaml
service: cable_modem_monitor.generate_dashboard
data:
  include_status_card: true
  include_downstream_power: true
  include_downstream_snr: true
  graph_hours: 48
  short_titles: true
response_variable: result
```

The YAML is in `result.yaml`.

---

## Manual Dashboard Example

If you prefer to build your dashboard by hand, here is a static example
showing all 24 downstream channels (typical for DOCSIS 3.0 modems),
upstream channels, and error tracking.

The full dashboard configuration is in [`examples/manual-dashboard.yaml`](examples/manual-dashboard.yaml) — a 167-line Lovelace YAML covering the status entities, downstream and upstream history graphs, and error totals. Copy it into your dashboard's Raw Configuration Editor as a starting point.

**Note**: This dashboard example includes all 24 downstream channels. If your modem has fewer channels (e.g., 16 or 8), simply remove the extra channel entries. If you have more channels, add them by following the same pattern with entity_ids like `sensor.cable_modem_ds_ch_X_power` where X is the channel number.

---

## Last Boot Time Display Options

The `sensor.cable_modem_last_boot_time` is a timestamp sensor. You can customize how it displays:

**Relative time (recommended)** - Compact and informative:

```yaml
- entity: sensor.cable_modem_last_boot_time
  format: relative
```

Output: `29 days ago`

**Date only** - Just the date:

```yaml
- entity: sensor.cable_modem_last_boot_time
  format: date
```

Output: `2025-09-25`

**Time only** - Just the time:

```yaml
- entity: sensor.cable_modem_last_boot_time
  format: time
```

Output: `00:38:00`

**Full datetime (fits in UI)** - Date and time:

```yaml
- entity: sensor.cable_modem_last_boot_time
  format: datetime
```

Output: `2025-09-25 00:38:00`

**Custom template** - For more control (may be too long for some UIs):

```yaml
type: markdown
content: >
  Last Reboot: {{
    as_timestamp(states('sensor.cable_modem_last_boot_time'))
    | timestamp_custom('%Y-%m-%d %H:%M')
  }}
```

Output: `Last Reboot: 2025-09-25 00:38`

---

## Automation Examples

### Alert on High Uncorrected Errors

```yaml
automation:
  - alias: "Cable Modem - High Uncorrected Errors"
    trigger:
      - platform: numeric_state
        entity_id: sensor.cable_modem_total_uncorrected_errors
        above: 100
    action:
      - service: notify.notify
        data:
          title: "Cable Modem Alert"
          message: "High uncorrected errors detected. Check your cable connection."
```

### Alert on Low SNR

```yaml
automation:
  - alias: "Cable Modem - Low SNR Warning"
    trigger:
      - platform: numeric_state
        entity_id: sensor.cable_modem_ds_ch_1_snr
        below: 30
    action:
      - service: notify.notify
        data:
          title: "Cable Modem Alert"
          message: "Low signal quality detected on downstream channel 1."
```

### Alert on Channel Count Changes

```yaml
automation:
  - alias: "Cable Modem - Channel Count Changed"
    trigger:
      - platform: state
        entity_id:
          - sensor.cable_modem_downstream_channel_count
          - sensor.cable_modem_upstream_channel_count
    condition:
      - condition: template
        value_template: "{{ trigger.from_state.state != 'unavailable' }}"
    action:
      - service: notify.notify
        data:
          title: "Cable Modem Alert"
          message: "Channel count changed: {{ trigger.to_state.name }} is now {{ trigger.to_state.state }}"
```

### Auto-Restart on Network Issues

```yaml
automation:
  - alias: "Cable Modem - Auto Restart on High Errors"
    trigger:
      - platform: numeric_state
        entity_id: sensor.cable_modem_total_uncorrected_errors
        above: 1000
    action:
      - service: notify.notify
        data:
          title: "Cable Modem Alert"
          message: "High error count detected. Restarting modem..."
      - service: button.press
        target:
          entity_id: button.cable_modem_restart_modem
```

### Modem Status Alert (v3.10.0+)

```yaml
automation:
  - alias: "Cable Modem Status Alert"
    trigger:
      - platform: state
        entity_id: sensor.cable_modem_status
        to: "Unresponsive"
        for:
          minutes: 5
    action:
      - service: notify.mobile_app
        data:
          title: "Modem Offline"
          message: "Cable modem is not responding. Check power and connections."

  - alias: "Cable Modem DOCSIS Alert"
    trigger:
      - platform: state
        entity_id: sensor.cable_modem_status
        to:
          - "Not Locked"
          - "Partial Lock"
        for:
          minutes: 10
    action:
      - service: notify.mobile_app
        data:
          title: "Modem Connection Issue"
          message: "Cable modem DOCSIS status: {{ states('sensor.cable_modem_status') }}"
```
