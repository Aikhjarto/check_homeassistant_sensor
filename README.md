# check_homeassistant_sensor

A Nagios/Icinga plugin for monitoring [Home Assistant](https://www.home-assistant.io/)
entities via its REST API. It is the Home Assistant sibling of
[check_esphome_sensor](https://github.com/Aikhjarto/check_esphome_sensor) and
[check_tasmota_sensor](https://github.com/Aikhjarto/check_tasmota_sensor), with
the same kinds of checks and options. Python 3.8 or newer, standard library
only.

## Features

- **Six kinds of checks** (`--check`)
  - `sensor`: compare a numeric entity state against `-w`/`-c` thresholds.
    Unlike most plugins, `-c` may be smaller than `-w`: whichever of the two
    is larger decides whether higher or lower values count as worse (e.g. a
    battery percentage where *lower* is bad).
  - `compare`: check that `--entity` stays above (or below) the
    `--compare-entity` by a margin, see [below](#comparing-two-entities).
  - `time`: compare a timestamp entity (a `device_class: timestamp` sensor or
    an `input_datetime`) against the local clock (default) or an NTP server
    (`--time-server`), in seconds of offset.
  - `age`: seconds since the entity last reported a value, to catch sensors
    that stopped updating.
  - `binary`: evaluate a boolean expression (`and`/`or`/`not`/parentheses)
    over on/off entities, e.g. `--binary-expr "front_door and not switch.alarm"`.
  - `text`: match an entity state against a Python regular expression
    (`--regex`), e.g. to check a status string.
- **`--attribute`**: check an attribute instead of the state, e.g. the
  `current_temperature` of a `climate` entity.
- **Unavailable entities**: an entity whose state is `unavailable` or
  `unknown` -- typically a device Home Assistant lost contact with -- is
  CRITICAL in every check except `age`; `--unavailable-state` changes that.
- **`--list`**: instead of `--check`, print all entities with their state and
  unit of measurement, so you know what to pass to `--entity`.

## Access token

Create a long-lived access token in Home Assistant under your user profile ->
*Security*. A dedicated non-admin user is enough, as the plugin only reads
states.

Never pass the token with `-T` in a service definition: command lines are
visible to every user via `ps`. Put it in a file that only the monitoring user
can read and pass it with `-f`, or set `$HASS_TOKEN_FILE` or `$HASS_TOKEN`.
Long-lived access tokens are valid for ten years.

## Usage

```console
$ check_homeassistant_sensor --help
```

| Option | Meaning |
|---|---|
| `-H`, `--host` | Home Assistant host name or address (required) |
| `-p`, `--port` | Port (default: 8123) |
| `-S`, `--ssl` | Connect with HTTPS; `--ca-cert` and `--insecure` control the certificate check |
| `-f`, `--token-file` | File holding the access token |
| `-t`, `--timeout` | Timeout in seconds (default: 10) |
| `--entity` | Entity id, e.g. `sensor.outside_temperature` |
| `-w`, `-c` | Thresholds; for `compare`, the smallest acceptable margins |

### Examples

```console
# Numeric threshold check
check_homeassistant_sensor -H hass.local -f hass.token --check sensor \
    --entity sensor.outside_temperature -w 30 -c 35

# Lower is worse: -c below -w
check_homeassistant_sensor -H hass.local -f hass.token --check sensor \
    --entity sensor.phone_battery_level -w 20 -c 10

# An attribute instead of the state
check_homeassistant_sensor -H hass.local -f hass.token --check sensor \
    --entity climate.living_room --attribute current_temperature -w 26 -c 28

# The wall has to stay at least 3 degrees above the dew point
check_homeassistant_sensor -H hass.local -f hass.token --check compare \
    --entity sensor.wall_temperature --compare-entity sensor.dew_point \
    --must-be above -w 3 -c 1

# A sensor that stopped reporting
check_homeassistant_sensor -H hass.local -f hass.token --check age \
    --entity sensor.outside_temperature -w 900 -c 3600

# Logical combination of on/off entities
check_homeassistant_sensor -H hass.local -f hass.token --check binary \
    --binary-expr 'front_door and not switch.alarm'

# Regex match against a state
check_homeassistant_sensor -H hass.local -f hass.token --check text \
    --entity update.home_assistant_core_update --regex '^off$'

# List all entities
check_homeassistant_sensor -H hass.local -f hass.token --list
```

### Comparing two entities

`--check compare` checks that one value stays on one side of another, as
climate control has to keep it: `--must-be above` requires `--entity` to be
larger than `--compare-entity`, `--must-be below` smaller. `-w` and `-c` are
the smallest acceptable margins between the two, in their unit of
measurement: a margin at or below `-w` is WARNING, at or below `-c` CRITICAL.
So `-w` must not be smaller than `-c`, and a value on the wrong side is
CRITICAL whenever `-c` is 0 or more.

- **Condensation**: a wall has to stay warmer than the dew point of the air:
  `--entity sensor.wall_temperature --compare-entity sensor.dew_point --must-be above -w 3 -c 1`
- **Air conditioning**: the room has to stay cooler than outside:
  `--entity sensor.room_temperature --compare-entity sensor.outside_temperature --must-be below -w 2 -c 0`

`--attribute` and `--compare-attribute` compare attributes instead of states,
e.g. the `current_temperature` and the target `temperature` of a `climate`
entity. The performance data holds the margin and both values. Entities with
different units of measurement, such as °C and °F, are UNKNOWN.

### Entity ids in `--binary-expr`

A bare name is a `binary_sensor`, so `front_door` means
`binary_sensor.front_door`. Other domains are written as `domain.name`, e.g.
`switch.alarm` or `input_boolean.away`, and entity ids that are no valid
Python names are quoted: `'"light.living-room" and not away'`. Every entity
in the expression has to be `on` or `off`. `--true-state` and
`--false-state` choose the result (default: CRITICAL if true, OK if false).

### Time zones

Timestamp sensors carry a UTC offset. `input_datetime` and the `time_date`
sensors report local time without one; the plugin takes it to be in its own
host's time zone, so run it in the time zone of Home Assistant.

## Output and exit codes

Every check prints one line: the state, a summary and the performance data.
Units of measurement are shown in the text; in the performance data only
those the [monitoring plugins guidelines](https://www.monitoring-plugins.org/doc/guidelines.html#AEN201)
allow (`s`, `%`, `B`, `c`, ...), unless `--uom` sets one explicitly.

An unreachable Home Assistant is CRITICAL. A rejected token, an unknown
entity, a value that is not a number and a wrong command line are UNKNOWN.

## Installation

```console
$ pip install .
```

This installs a `check_homeassistant_sensor` console script.

## Bash completion

`completions/check_homeassistant_sensor` completes the options and their
values, also when the plugin is called by its full path. The packages install
it; otherwise copy it to bash-completion's directory:

```sh
install -D -m 0644 completions/check_homeassistant_sensor /usr/share/bash-completion/completions/check_homeassistant_sensor
```

## Tests

```console
$ PYTHONPATH=src python3 -m unittest -v
```

The tests run the plugin against a fake Home Assistant REST API.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
