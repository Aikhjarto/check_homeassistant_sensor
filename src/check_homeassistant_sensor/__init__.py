# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""check_homeassistant_sensor - Nagios/Icinga plugin for Home Assistant entities.

Reads entity states via Home Assistant's REST API, authenticated with a
long-lived access token (create one under your user profile -> Security).

Six kinds of checks (--check):
  sensor  Compare a numeric entity state (or --attribute) against -w/-c
          thresholds. Unlike most plugins, -c may be smaller than -w:
          whichever of the two is larger decides whether higher or lower
          values are considered worse.
  time    Compare a timestamp entity (e.g. a device_class timestamp
          sensor or an input_datetime) against the local clock (default)
          or an NTP server (--time-server), in seconds of offset.
  age     Seconds since the entity last reported a value, to catch
          sensors that stopped updating.
  binary  Evaluate a boolean expression (and/or/not/parentheses) over
          on/off entities, e.g. --binary-expr "door and not switch.alarm".
          A bare name is a binary_sensor; write domain.name for other
          domains, or quote entity ids that are no valid Python names.
  text    Match an entity state (or --attribute) against a Python regular
          expression (--regex).
  compare Check that --entity stays above (or below, --must-be) the
          --compare-entity by a margin: -w and -c are the smallest
          acceptable margins, e.g. a wall temperature at least 3 degrees
          above the dew point (--must-be above -w 3 -c 1), or a room at
          least 2 degrees below the outside temperature for an air
          conditioning (--must-be below -w 2 -c 0).

An entity whose state is "unavailable" or "unknown" -- typically a device
Home Assistant lost contact with -- is reported with --unavailable-state
(default: critical) by every check except age.

Pass --list instead of --check to print all entities with their state and
unit of measurement, so you know what to pass to --entity.
"""
import argparse
import ast
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

__version__ = "0.2.1"

STATE_OK = 0
STATE_WARNING = 1
STATE_CRITICAL = 2
STATE_UNKNOWN = 3
STATE_NAMES = {
    STATE_OK: "OK",
    STATE_WARNING: "WARNING",
    STATE_CRITICAL: "CRITICAL",
    STATE_UNKNOWN: "UNKNOWN",
}
STATE_BY_NAME = {"ok": STATE_OK, "warning": STATE_WARNING, "critical": STATE_CRITICAL, "unknown": STATE_UNKNOWN}

NTP_EPOCH_OFFSET = 2208988800  # seconds between 1900-01-01 and 1970-01-01

# states Home Assistant reports when it has no value for an entity
UNAVAILABLE_STATES = ("unavailable", "unknown")

# units of measurement the monitoring plugins guidelines allow in perfdata
PERFDATA_UOMS = ("s", "ms", "us", "%", "B", "KB", "MB", "GB", "TB", "c")

AGE_FIELDS = {"reported": "last_reported", "updated": "last_updated", "changed": "last_changed"}


def die(state, message):
    print(f"{STATE_NAMES[state]}: {message}")
    sys.exit(state)


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

def read_token(args):
    """The token from -f, $HASS_TOKEN_FILE, -T or $HASS_TOKEN, in this order."""
    path = args.token_file or os.environ.get("HASS_TOKEN_FILE")
    if path:
        try:
            with open(path) as handle:
                token = handle.read().strip()
        except OSError as exc:
            die(STATE_UNKNOWN, f"Could not read token file {path}: {exc.strerror}")
        if not token:
            die(STATE_UNKNOWN, f"Token file {path} is empty")
        return token
    token = args.token or os.environ.get("HASS_TOKEN")
    if not token:
        die(STATE_UNKNOWN, "No access token given, use -f/--token-file")
    return token


def base_url(args):
    scheme = "https" if args.ssl else "http"
    host = f"[{args.host}]" if ":" in args.host else args.host
    return f"{scheme}://{host}:{args.port}"


def api_get(args, path):
    """GET a path of the REST API and return the decoded JSON, or None for a 404."""
    url = base_url(args) + path
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {args.token_value}",
        "Accept": "application/json",
        "User-Agent": f"check_homeassistant_sensor/{__version__}",
    })
    context = None
    if args.ssl:
        context = ssl.create_default_context(cafile=args.ca_cert)
        if args.insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=args.timeout, context=context) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (401, 403):
            die(STATE_UNKNOWN, f"Home Assistant rejected the access token (HTTP {exc.code})")
        die(STATE_CRITICAL, f"HTTP request to {url} returned status {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        die(STATE_CRITICAL, f"HTTP request to {url} failed: {reason}")
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        die(STATE_UNKNOWN, f"Could not parse JSON response from {url}")


def fetch_entity(args, entity_id):
    data = api_get(args, "/api/states/" + urllib.parse.quote(entity_id, safe=""))
    if data is None:
        die(STATE_UNKNOWN, f"Unknown entity: {entity_id}")
    if not isinstance(data, dict) or "state" not in data:
        die(STATE_UNKNOWN, f"Unexpected response for {entity_id}: {data!r}")
    return data


def check_available(args, entity_id, data):
    """Report an entity Home Assistant has no value for with --unavailable-state."""
    if data.get("state") in UNAVAILABLE_STATES:
        die(STATE_BY_NAME[args.unavailable_state], f"{entity_id} is {data['state']}")


def entity_value(args, entity_id, data):
    """The state, or the attribute given with --attribute."""
    check_available(args, entity_id, data)
    if not args.attribute:
        return data["state"]
    attributes = data.get("attributes") or {}
    if args.attribute not in attributes:
        die(STATE_UNKNOWN, f"{entity_id} has no attribute {args.attribute!r}")
    return attributes[args.attribute]


def value_label(args, entity_id, attribute=None):
    attribute = attribute or args.attribute
    return f"{entity_id}.{attribute}" if attribute else entity_id


def numeric_value(args, entity_id, attribute=None):
    """The numeric state (or attribute) of an entity and its unit of measurement."""
    data = fetch_entity(args, entity_id)
    check_available(args, entity_id, data)
    if attribute:
        attributes = data.get("attributes") or {}
        if attribute not in attributes:
            die(STATE_UNKNOWN, f"{entity_id} has no attribute {attribute!r}")
        value, uom = attributes[attribute], ""
    else:
        value = data["state"]
        uom = (data.get("attributes") or {}).get("unit_of_measurement") or ""
    try:
        return float(value), uom
    except (TypeError, ValueError):
        die(STATE_UNKNOWN, f"Value of {value_label(args, entity_id, attribute)} is not numeric: {value!r}")


# ---------------------------------------------------------------------------
# entity listing (--list)
# ---------------------------------------------------------------------------

def list_entities(args):
    data = api_get(args, "/api/states")
    if not isinstance(data, list):
        die(STATE_UNKNOWN, "Unexpected response for /api/states")
    return data


def print_entity_list(entities, host):
    print(f"Available entities on {host}:")
    if not entities:
        print("  (none)")
        return
    by_domain = {}
    for entity in entities:
        entity_id = entity.get("entity_id", "")
        domain = entity_id.split(".", 1)[0]
        by_domain.setdefault(domain, []).append(entity)
    for domain in sorted(by_domain):
        print(f"  {domain}:")
        for entity in sorted(by_domain[domain], key=lambda item: item.get("entity_id", "")):
            uom = (entity.get("attributes") or {}).get("unit_of_measurement")
            value = entity.get("state")
            print(f"    {entity.get('entity_id')} = {value}" + (f" {uom}" if uom else ""))


# ---------------------------------------------------------------------------
# threshold evaluation (direction-agnostic: -c may be smaller than -w)
# ---------------------------------------------------------------------------

def format_value(value):
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6g}"


def evaluate_numeric(value, warning, critical, label, uom="", perf_uom=None):
    """uom is shown in the text, perf_uom (default: uom) in the performance data."""
    if warning is not None and critical is not None and warning > critical:
        # lower values are worse (e.g. battery / signal level style checks)
        if value <= critical:
            state = STATE_CRITICAL
        elif value <= warning:
            state = STATE_WARNING
        else:
            state = STATE_OK
    else:
        # higher values are worse (the common case)
        if critical is not None and value >= critical:
            state = STATE_CRITICAL
        elif warning is not None and value >= warning:
            state = STATE_WARNING
        else:
            state = STATE_OK

    perf_uom = uom if perf_uom is None else perf_uom
    fields = ["" if warning is None else format_value(warning), "" if critical is None else format_value(critical)]
    while fields and fields[-1] == "":
        fields.pop()
    perf = f"'{label}'={format_value(value)}{perf_uom}" + "".join(";" + field for field in fields)
    shown_uom = f" {uom}" if uom and uom not in ("%", "s") else uom
    message = f"{label} is {format_value(value)}{shown_uom} | {perf}"
    return state, message


def evaluate_margin(value, other, must_be, warning, critical, label, other_label, uom="", perf_uom=""):
    """--check compare: value has to be above (or below) other by more than
    the margins -w and -c; a margin at or below -c is CRITICAL."""
    margin = value - other if must_be == "above" else other - value
    if margin <= critical:
        state = STATE_CRITICAL
    elif margin <= warning:
        state = STATE_WARNING
    else:
        state = STATE_OK
    shown_uom = f" {uom}" if uom and uom not in ("%", "s") else uom
    if margin > 0:
        relation = f"{format_value(abs(margin))}{shown_uom} {must_be}"
    elif margin == 0:
        relation = f"equal to, not {must_be},"
    else:
        relation = f"{format_value(abs(margin))}{shown_uom} {'below' if must_be == 'above' else 'above'}, not {must_be},"
    message = (f"{label} is {relation} {other_label} ({format_value(value)}{shown_uom} vs "
               f"{format_value(other)}{shown_uom}) | 'margin'={format_value(margin)}{perf_uom};"
               f"{format_value(warning)};{format_value(critical)} '{label}'={format_value(value)}{perf_uom} "
               f"'{other_label}'={format_value(other)}{perf_uom}")
    return state, message


# ---------------------------------------------------------------------------
# time-offset and age checks
# ---------------------------------------------------------------------------

def query_ntp(server, timeout):
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (server, 123))
            data, _ = sock.recvfrom(48)
        except OSError as exc:
            die(STATE_UNKNOWN, f"Could not query NTP server {server}: {exc}")
    if len(data) < 48:
        die(STATE_UNKNOWN, f"Short NTP response from {server}")
    transmit_timestamp = struct.unpack("!12I", data)[10]
    return transmit_timestamp - NTP_EPOCH_OFFSET


def get_reference_time(time_server, timeout):
    if not time_server:
        return time.time()
    return query_ntp(time_server, timeout)


def parse_timestamp(value):
    """A Unix timestamp, or an ISO 8601 string. Timestamp sensors carry a UTC
    offset; a naive string, as input_datetime and the time_date sensors
    report, is local time, which is taken to be this host's timezone."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        # Python < 3.11 does not accept the trailing Z of UTC
        dt = datetime.fromisoformat(re.sub(r"Z$", "+00:00", text))
    except ValueError:
        die(STATE_UNKNOWN, f"Could not parse time value: {text!r}")
    return dt.timestamp()


# ---------------------------------------------------------------------------
# on/off logical expression check
# ---------------------------------------------------------------------------

ALLOWED_EXPR_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Name,
    ast.Attribute,
    ast.Load,
    ast.Constant,
)

BINARY_STATES = {"on": True, "off": False}


def parse_bool_expr(expr):
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        die(STATE_UNKNOWN, f"Invalid --binary-expr: {exc}")

    def check(node):
        if not isinstance(node, ALLOWED_EXPR_NODES):
            die(STATE_UNKNOWN, f"Unsupported element in --binary-expr: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and not isinstance(node.value, ast.Name):
            die(STATE_UNKNOWN, "Unsupported element in --binary-expr: write entity ids as domain.name")
        for child in ast.iter_child_nodes(node):
            check(child)

    check(tree)
    return tree


def entity_id_of(node):
    """The entity id a name in the expression stands for: domain.name as
    written, a bare name is a binary_sensor, a string is taken literally."""
    if isinstance(node, ast.Attribute):
        return f"{node.value.id}.{node.attr}"
    if isinstance(node, ast.Name):
        return f"binary_sensor.{node.id}"
    return node.value


def extract_names(tree):
    names = set()

    def walk(node):
        if isinstance(node, ast.Attribute) or isinstance(node, ast.Name) or (
                isinstance(node, ast.Constant) and isinstance(node.value, str)):
            names.add(entity_id_of(node))
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return sorted(names)


def eval_bool_expr(tree, values):
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BoolOp):
            results = [ev(v) for v in node.values]
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.UnaryOp):
            return not ev(node.operand)
        if isinstance(node, ast.Constant) and not isinstance(node.value, str):
            return bool(node.value)
        return values[entity_id_of(node)]

    return ev(tree)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

class ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which Nagios reads as CRITICAL; exit UNKNOWN instead."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(STATE_UNKNOWN, f"{self.prog}: error: {message}\n")


def parse_args(argv=None):
    parser = ArgumentParser(
        prog="check_homeassistant_sensor",
        description="Check Home Assistant entities via its REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -H hass.local -f /etc/icinga2/hass.token --check sensor "
            "--entity sensor.outside_temperature -w 30 -c 35\n"
            "  %(prog)s -H hass.local -f hass.token --check sensor --entity sensor.phone_battery_level "
            "-w 20 -c 10\n"
            "  %(prog)s -H hass.local -f hass.token --check age --entity sensor.outside_temperature "
            "-w 900 -c 3600\n"
            "  %(prog)s -H hass.local -f hass.token --check binary --binary-expr "
            "'front_door and not switch.alarm'\n"
            "  %(prog)s -H hass.local -f hass.token --check text --entity update.home_assistant_core_update "
            "--regex '^off$'\n"
            "  %(prog)s -H hass.local -f hass.token --check compare --entity sensor.wall_temperature "
            "--compare-entity sensor.dew_point --must-be above -w 3 -c 1\n"
            "  %(prog)s -H hass.local -f hass.token --list\n"
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-H", "--host", required=True, help="Home Assistant hostname or IP address")
    parser.add_argument("-p", "--port", type=int, default=8123, help="port to connect to (default: 8123)")
    parser.add_argument("-S", "--ssl", action="store_true", help="connect with HTTPS")
    parser.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
    parser.add_argument("--ca-cert", help="CA bundle to verify the TLS certificate with")
    parser.add_argument("-t", "--timeout", type=float, default=10, help="timeout in seconds (default: 10)")

    token_group = parser.add_argument_group("access token")
    token_group.add_argument("-f", "--token-file",
                             help="file holding a long-lived access token (also: $HASS_TOKEN_FILE)")
    token_group.add_argument("-T", "--token",
                             help="long-lived access token (also: $HASS_TOKEN); visible in the process list, "
                                  "prefer -f")

    parser.add_argument("--list", action="store_true",
                        help="list all entities with their state and exit, instead of running a check")
    parser.add_argument("--check", choices=("sensor", "time", "age", "binary", "text", "compare"),
                        help="type of check to perform")
    parser.add_argument("--entity", help="entity id, e.g. sensor.outside_temperature")
    parser.add_argument("--attribute", help="check this attribute instead of the state (sensor, time and text)")
    parser.add_argument("--unavailable-state", choices=("ok", "warning", "critical", "unknown"),
                        default="critical",
                        help="state if an entity is unavailable or unknown (default: critical)")
    parser.add_argument("-w", "--warning", type=float, help="warning threshold (may be > or < -c)")
    parser.add_argument("-c", "--critical", type=float, help="critical threshold (may be > or < -w)")

    sensor_group = parser.add_argument_group("--check sensor")
    sensor_group.add_argument(
        "--uom", default="",
        help="unit of measurement for performance data (default: the entity's unit_of_measurement, "
             "if it is one the monitoring plugins guidelines allow)",
    )

    time_group = parser.add_argument_group("--check time")
    time_group.add_argument("--time-server", help="NTP server to compare against (default: local clock)")

    compare_group = parser.add_argument_group("--check compare")
    compare_group.add_argument("--compare-entity", help="entity id --entity is compared with")
    compare_group.add_argument("--compare-attribute",
                               help="compare with this attribute of --compare-entity instead of its state")
    compare_group.add_argument(
        "--must-be", choices=("above", "below"),
        help="whether --entity has to stay above or below --compare-entity; -w and -c are the smallest "
             "acceptable margins, so -w must not be smaller than -c",
    )

    age_group = parser.add_argument_group("--check age")
    age_group.add_argument(
        "--age-of", choices=tuple(AGE_FIELDS), default="reported",
        help="reported: the last time the entity reported a value, even an unchanged one (default; "
             "Home Assistant 2024.3 or newer, older versions fall back to updated); updated: the "
             "last change of state or attributes; changed: the last change of the state",
    )

    binary_group = parser.add_argument_group("--check binary")
    binary_group.add_argument(
        "--binary-expr",
        help="boolean expression over on/off entities, e.g. 'front_door and not switch.alarm'; a bare "
             "name is a binary_sensor, quote entity ids that are no valid Python names",
    )
    binary_group.add_argument("--true-state", choices=("ok", "warning", "critical"), default="critical")
    binary_group.add_argument("--false-state", choices=("ok", "warning", "critical"), default="ok")

    text_group = parser.add_argument_group("--check text")
    text_group.add_argument("--regex", help="Python regular expression to search for in the value")
    text_group.add_argument("-i", "--ignore-case", action="store_true", help="match --regex case-insensitively")
    text_group.add_argument("--match-state", choices=("ok", "warning", "critical"), default="ok")
    text_group.add_argument("--no-match-state", choices=("ok", "warning", "critical"), default="critical")

    args = parser.parse_args(argv)

    if args.list:
        return args

    if not args.check:
        parser.error("--check is required (or use --list)")

    if args.check in ("sensor", "time", "age"):
        if not args.entity or args.warning is None or args.critical is None:
            parser.error(f"--check {args.check} requires --entity, -w and -c")
    elif args.check == "compare":
        if not args.entity or not args.compare_entity or not args.must_be or args.warning is None \
                or args.critical is None:
            parser.error("--check compare requires --entity, --compare-entity, --must-be, -w and -c")
        if args.warning < args.critical:
            parser.error("for --check compare, -w and -c are the smallest acceptable margins, "
                         "so -w must not be smaller than -c")
    elif args.check == "binary":
        if not args.binary_expr:
            parser.error("--check binary requires --binary-expr")
    elif args.check == "text":
        if not args.entity or not args.regex:
            parser.error("--check text requires --entity and --regex")
        try:
            re.compile(args.regex, re.IGNORECASE if args.ignore_case else 0)
        except re.error as exc:
            parser.error(f"Invalid --regex: {exc}")

    return args


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    args = parse_args(argv)
    args.token_value = read_token(args)

    if args.list:
        print_entity_list(list_entities(args), args.host)
        sys.exit(STATE_OK)

    if args.check == "sensor":
        data = fetch_entity(args, args.entity)
        value = entity_value(args, args.entity, data)
        try:
            value = float(value)
        except (TypeError, ValueError):
            die(STATE_UNKNOWN, f"Value of {value_label(args, args.entity)} is not numeric: {value!r}")
        if args.uom:
            uom = perf_uom = args.uom
        else:
            uom = "" if args.attribute else ((data.get("attributes") or {}).get("unit_of_measurement") or "")
            perf_uom = uom if uom in PERFDATA_UOMS else ""
        state, message = evaluate_numeric(value, args.warning, args.critical, value_label(args, args.entity),
                                          uom, perf_uom)
        die(state, message)

    elif args.check == "compare":
        value, uom = numeric_value(args, args.entity, args.attribute)
        other, other_uom = numeric_value(args, args.compare_entity, args.compare_attribute)
        if uom and other_uom and uom != other_uom and not args.uom:
            die(STATE_UNKNOWN, f"{args.entity} is in {uom}, but {args.compare_entity} in {other_uom}")
        uom = args.uom or uom or other_uom
        perf_uom = uom if args.uom or uom in PERFDATA_UOMS else ""
        state, message = evaluate_margin(value, other, args.must_be, args.warning, args.critical,
                                         value_label(args, args.entity),
                                         value_label(args, args.compare_entity, args.compare_attribute),
                                         uom, perf_uom)
        die(state, message)

    elif args.check == "time":
        data = fetch_entity(args, args.entity)
        device_epoch = parse_timestamp(entity_value(args, args.entity, data))
        reference_epoch = get_reference_time(args.time_server, args.timeout)
        offset = abs(device_epoch - reference_epoch)
        label = f"time offset of {value_label(args, args.entity)}"
        state, message = evaluate_numeric(offset, args.warning, args.critical, label, "s")
        die(state, message)

    elif args.check == "age":
        data = fetch_entity(args, args.entity)
        field = AGE_FIELDS[args.age_of]
        stamp = data.get(field) or (data.get("last_updated") if args.age_of == "reported" else None)
        if not stamp:
            die(STATE_UNKNOWN, f"{args.entity} has no {field}")
        age = max(0.0, time.time() - parse_timestamp(stamp))
        state, message = evaluate_numeric(round(age), args.warning, args.critical, f"age of {args.entity}", "s")
        die(state, message)

    elif args.check == "binary":
        tree = parse_bool_expr(args.binary_expr)
        names = extract_names(tree)
        if not names:
            die(STATE_UNKNOWN, "No entity names found in --binary-expr")
        values = {}
        for name in names:
            data = fetch_entity(args, name)
            check_available(args, name, data)
            state_text = str(data["state"]).lower()
            if state_text not in BINARY_STATES:
                die(STATE_UNKNOWN, f"{name} is {data['state']!r}, not on or off")
            values[name] = BINARY_STATES[state_text]
        result = eval_bool_expr(tree, values)
        state = STATE_BY_NAME[args.true_state if result else args.false_state]
        details = ", ".join(f"{n}={'ON' if values[n] else 'OFF'}" for n in names)
        die(state, f"'{args.binary_expr}' is {result} ({details})")

    elif args.check == "text":
        data = fetch_entity(args, args.entity)
        text = str(entity_value(args, args.entity, data))
        flags = re.IGNORECASE if args.ignore_case else 0
        matched = re.search(args.regex, text, flags) is not None
        state = STATE_BY_NAME[args.match_state if matched else args.no_match_state]
        label = value_label(args, args.entity)
        die(state, f"{label} = {text!r} {'matches' if matched else 'does not match'} /{args.regex}/")


def run():
    try:
        main()
    except KeyboardInterrupt:
        die(STATE_UNKNOWN, "Interrupted")


if __name__ == "__main__":
    run()
