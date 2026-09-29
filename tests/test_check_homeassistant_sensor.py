"""Tests for check_homeassistant_sensor against a fake Home Assistant REST API."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import check_homeassistant_sensor as plugin

TOKEN = "test-token"
SRC = os.path.dirname(os.path.dirname(os.path.abspath(plugin.__file__)))


def iso(seconds_ago, naive=False):
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    if naive:
        return moment.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    return moment.isoformat()


def entity(entity_id, state, seconds_ago=5, **attributes):
    return {
        "entity_id": entity_id, "state": state, "attributes": attributes,
        "last_changed": iso(seconds_ago + 3000), "last_updated": iso(seconds_ago + 100),
        "last_reported": iso(seconds_ago),
    }


def entities():
    old = entity("sensor.stuck", "12", 7200, unit_of_measurement="°C")
    del old["last_reported"]  # Home Assistant before 2024.3
    return [
        entity("sensor.outside_temperature", "21.5", unit_of_measurement="°C", friendly_name="Outside"),
        entity("sensor.phone_battery_level", "15", unit_of_measurement="%"),
        entity("sensor.power", "1500", unit_of_measurement="W"),
        entity("sensor.offline", "unavailable", unit_of_measurement="°C"),
        entity("sensor.firmware", "1.2.3 ok"),
        entity("sensor.last_boot", iso(3)),
        entity("input_datetime.alarm", iso(0, naive=True), has_date=True, has_time=True),
        entity("climate.living_room", "heat", current_temperature=19.5, temperature=21),
        entity("binary_sensor.front_door", "off", device_class="door"),
        entity("binary_sensor.window", "on", device_class="window"),
        entity("switch.alarm", "on"),
        entity("binary_sensor.lost", "unknown"),
        entity("person.alice", "home"),
        entity("sensor.wall", "15.1", unit_of_measurement="°C"),
        entity("sensor.dew_point", "11.9", unit_of_measurement="°C"),
        entity("sensor.room", "24", unit_of_measurement="°C"),
        entity("sensor.outside", "30", unit_of_measurement="°C"),
        entity("sensor.outside_f", "86", unit_of_measurement="°F"),
        entity("sensor.humidity_a", "40", unit_of_measurement="%"),
        entity("sensor.humidity_b", "45", unit_of_measurement="%"),
        old,
    ]


class FakeHomeAssistant(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.reply(401, {"message": "Unauthorized"})
        if self.path == "/api/states":
            return self.reply(200, entities())
        prefix = "/api/states/"
        if self.path.startswith(prefix):
            wanted = self.path[len(prefix):]
            for item in entities():
                if item["entity_id"] == wanted:
                    return self.reply(200, item)
            return self.reply(404, {"message": "Entity not found."})
        if self.path == "/api/broken":
            return self.reply(500, {"message": "boom"})
        return self.reply(404, {"message": "Not found"})


class TestPlugin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeHomeAssistant)
        cls.server.requests = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        handle, cls.token_file = tempfile.mkstemp()
        os.write(handle, (TOKEN + "\n").encode())
        os.close(handle)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.remove(cls.token_file)

    def setUp(self):
        self.server.requests.clear()

    def run_plugin(self, *argv, token_file=None, env=None):
        environ = dict(os.environ, PYTHONPATH=SRC)
        environ.pop("HASS_TOKEN", None)
        environ.pop("HASS_TOKEN_FILE", None)
        environ.update(env or {})
        command = [sys.executable, "-m", "check_homeassistant_sensor", "-H", "127.0.0.1", "-p", str(self.port)]
        if token_file is not False:
            command += ["-f", token_file or self.token_file]
        result = subprocess.run(command + list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, env=environ, timeout=60)
        if "--list" not in argv and (result.returncode != plugin.STATE_UNKNOWN or result.stdout):
            self.assertEqual(1, len(result.stdout.splitlines()), result)
        return result

    def expect(self, state, pattern, *argv, **kwargs):
        result = self.run_plugin(*argv, **kwargs)
        self.assertEqual(state, result.returncode, result)
        self.assertRegex(result.stdout, pattern)
        return result

    # --check sensor

    def test_sensor_ok(self):
        self.expect(plugin.STATE_OK, r"^OK: sensor.outside_temperature is 21.5 °C \| "
                                     r"'sensor.outside_temperature'=21.5;30;35$",
                    "--check", "sensor", "--entity", "sensor.outside_temperature", "-w", "30", "-c", "35")
        self.assertEqual([("/api/states/sensor.outside_temperature", f"Bearer {TOKEN}")], self.server.requests)

    def test_sensor_higher_is_worse(self):
        self.expect(plugin.STATE_WARNING, r"^WARNING: ", "--check", "sensor", "--entity",
                    "sensor.outside_temperature", "-w", "20", "-c", "25")
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: ", "--check", "sensor", "--entity",
                    "sensor.outside_temperature", "-w", "10", "-c", "20")

    def test_sensor_lower_is_worse_with_percent_in_perfdata(self):
        self.expect(plugin.STATE_WARNING, r"^WARNING: sensor.phone_battery_level is 15% \| "
                                          r"'sensor.phone_battery_level'=15%;20;10$",
                    "--check", "sensor", "--entity", "sensor.phone_battery_level", "-w", "20", "-c", "10")

    def test_sensor_uom_override(self):
        self.expect(plugin.STATE_OK, r"'sensor.power'=1500W;2000;3000$", "--check", "sensor", "--entity",
                    "sensor.power", "-w", "2000", "-c", "3000", "--uom", "W")

    def test_sensor_attribute(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: climate.living_room.current_temperature is 19.5 \| "
                                           r"'climate.living_room.current_temperature'=19.5;20;19.5$",
                    "--check", "sensor", "--entity", "climate.living_room", "--attribute",
                    "current_temperature", "-w", "20", "-c", "19.5")

    def test_missing_attribute(self):
        self.expect(plugin.STATE_UNKNOWN, r"has no attribute 'humidity'", "--check", "sensor", "--entity",
                    "climate.living_room", "--attribute", "humidity", "-w", "1", "-c", "2")

    def test_sensor_not_numeric(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: Value of sensor.firmware is not numeric: '1.2.3 ok'$",
                    "--check", "sensor", "--entity", "sensor.firmware", "-w", "1", "-c", "2")

    def test_unavailable(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: sensor.offline is unavailable$",
                    "--check", "sensor", "--entity", "sensor.offline", "-w", "1", "-c", "2")
        self.expect(plugin.STATE_WARNING, r"^WARNING: sensor.offline is unavailable$",
                    "--check", "sensor", "--entity", "sensor.offline", "-w", "1", "-c", "2",
                    "--unavailable-state", "warning")

    def test_unknown_entity(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: Unknown entity: sensor.nothing$",
                    "--check", "sensor", "--entity", "sensor.nothing", "-w", "1", "-c", "2")

    # --check time

    def test_time_with_utc_offset(self):
        self.expect(plugin.STATE_OK, r"^OK: time offset of sensor.last_boot is [0-9.]+s \| "
                                     r"'time offset of sensor.last_boot'=[0-9.]+s;10;60$",
                    "--check", "time", "--entity", "sensor.last_boot", "-w", "10", "-c", "60")
        self.expect(plugin.STATE_WARNING, r"^WARNING: ",
                    "--check", "time", "--entity", "sensor.last_boot", "-w", "1", "-c", "60")

    def test_time_naive_local(self):
        self.expect(plugin.STATE_OK, r"^OK: time offset of input_datetime.alarm is [0-9.]+s",
                    "--check", "time", "--entity", "input_datetime.alarm", "-w", "10", "-c", "60")

    # --check age

    def test_age_last_reported(self):
        self.expect(plugin.STATE_OK, r"^OK: age of sensor.outside_temperature is [0-9]s \| "
                                     r"'age of sensor.outside_temperature'=[0-9]s;60;600$",
                    "--check", "age", "--entity", "sensor.outside_temperature", "-w", "60", "-c", "600")

    def test_age_of_updated_and_changed(self):
        self.expect(plugin.STATE_WARNING, r"is 10[0-9]s", "--check", "age", "--entity",
                    "sensor.outside_temperature", "-w", "60", "-c", "600", "--age-of", "updated")
        self.expect(plugin.STATE_CRITICAL, r"is 30[0-9][0-9]s", "--check", "age", "--entity",
                    "sensor.outside_temperature", "-w", "60", "-c", "600", "--age-of", "changed")

    def test_age_falls_back_to_last_updated(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: age of sensor.stuck is 73[0-9][0-9]s",
                    "--check", "age", "--entity", "sensor.stuck", "-w", "900", "-c", "3600")

    def test_age_of_unavailable_entity(self):
        # age tells how long ago the entity reported, whatever it reported
        self.expect(plugin.STATE_OK, r"^OK: age of sensor.offline", "--check", "age", "--entity",
                    "sensor.offline", "-w", "60", "-c", "600")

    # --check binary

    def test_binary(self):
        self.expect(plugin.STATE_OK, r"^OK: 'front_door and not switch.alarm' is False "
                                     r"\(binary_sensor.front_door=OFF, switch.alarm=ON\)$",
                    "--check", "binary", "--binary-expr", "front_door and not switch.alarm")
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: 'window or front_door' is True",
                    "--check", "binary", "--binary-expr", "window or front_door")

    def test_binary_quoted_entity_and_states(self):
        self.expect(plugin.STATE_WARNING, r"is True \(switch.alarm=ON\)$", "--check", "binary",
                    "--binary-expr", '"switch.alarm"', "--true-state", "warning")

    def test_binary_not_on_off(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: person.alice is 'home', not on or off$",
                    "--check", "binary", "--binary-expr", "person.alice")

    def test_binary_unavailable(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: binary_sensor.lost is unknown$",
                    "--check", "binary", "--binary-expr", "window and lost")

    def test_binary_rejects_code(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: Unsupported element in --binary-expr: Call$",
                    "--check", "binary", "--binary-expr", "__import__('os')")
        self.expect(plugin.STATE_UNKNOWN, r"write entity ids as domain.name",
                    "--check", "binary", "--binary-expr", "a.b.c")

    # --check text

    def test_text(self):
        self.expect(plugin.STATE_OK, r"^OK: sensor.firmware = '1.2.3 ok' matches /ok\$/$",
                    "--check", "text", "--entity", "sensor.firmware", "--regex", "ok$")
        self.expect(plugin.STATE_CRITICAL, r"does not match /\^OK\$/$",
                    "--check", "text", "--entity", "sensor.firmware", "--regex", "^OK$")
        self.expect(plugin.STATE_OK, r"matches", "--check", "text", "--entity", "sensor.firmware",
                    "--regex", "OK$", "-i")

    def test_text_attribute(self):
        self.expect(plugin.STATE_OK, r"^OK: binary_sensor.window.device_class = 'window' matches",
                    "--check", "text", "--entity", "binary_sensor.window", "--attribute", "device_class",
                    "--regex", "^window$")

    # --check compare

    def test_compare_dew_point(self):
        # the wall has to stay warmer than the dew point, 3.2 degrees above it is fine
        self.expect(plugin.STATE_OK, r"^OK: sensor.wall is 3.2 °C above sensor.dew_point \(15.1 °C vs 11.9 °C\) "
                                     r"\| 'margin'=3.2;3;1 'sensor.wall'=15.1 'sensor.dew_point'=11.9$",
                    "--check", "compare", "--entity", "sensor.wall", "--compare-entity", "sensor.dew_point",
                    "--must-be", "above", "-w", "3", "-c", "1")

    def test_compare_close(self):
        self.expect(plugin.STATE_WARNING, r"^WARNING: sensor.wall is 3.2 °C above", "--check", "compare",
                    "--entity", "sensor.wall", "--compare-entity", "sensor.dew_point", "--must-be", "above",
                    "-w", "4", "-c", "1")
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: sensor.wall is 3.2 °C above", "--check", "compare",
                    "--entity", "sensor.wall", "--compare-entity", "sensor.dew_point", "--must-be", "above",
                    "-w", "5", "-c", "3.2")

    def test_compare_wrong_side(self):
        # the dew point above the wall temperature: condensation
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: sensor.dew_point is 3.2 °C below, not above, sensor.wall "
                                           r"\(11.9 °C vs 15.1 °C\) \| 'margin'=-3.2;3;1 ",
                    "--check", "compare", "--entity", "sensor.dew_point", "--compare-entity", "sensor.wall",
                    "--must-be", "above", "-w", "3", "-c", "1")

    def test_compare_below(self):
        # air conditioning: the room has to stay cooler than outside
        self.expect(plugin.STATE_OK, r"^OK: sensor.room is 6 °C below sensor.outside \(24 °C vs 30 °C\) "
                                     r"\| 'margin'=6;2;0 ",
                    "--check", "compare", "--entity", "sensor.room", "--compare-entity", "sensor.outside",
                    "--must-be", "below", "-w", "2", "-c", "0")
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: sensor.outside is 6 °C above, not below, sensor.room",
                    "--check", "compare", "--entity", "sensor.outside", "--compare-entity", "sensor.room",
                    "--must-be", "below", "-w", "2", "-c", "0")

    def test_compare_equal_is_critical_with_zero_margin(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: sensor.room is equal to, not below, sensor.room",
                    "--check", "compare", "--entity", "sensor.room", "--compare-entity", "sensor.room",
                    "--must-be", "below", "-w", "2", "-c", "0")

    def test_compare_percent_in_perfdata(self):
        self.expect(plugin.STATE_OK, r"'margin'=5%;2;1 'sensor.humidity_a'=40% 'sensor.humidity_b'=45%$",
                    "--check", "compare", "--entity", "sensor.humidity_a", "--compare-entity",
                    "sensor.humidity_b", "--must-be", "below", "-w", "2", "-c", "1")

    def test_compare_attribute(self):
        self.expect(plugin.STATE_OK, r"^OK: climate.living_room.current_temperature is 1.5 below "
                                     r"climate.living_room.temperature ",
                    "--check", "compare", "--entity", "climate.living_room", "--attribute", "current_temperature",
                    "--compare-entity", "climate.living_room", "--compare-attribute", "temperature",
                    "--must-be", "below", "-w", "1", "-c", "0")

    def test_compare_different_units(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: sensor.room is in °C, but sensor.outside_f in °F$",
                    "--check", "compare", "--entity", "sensor.room", "--compare-entity", "sensor.outside_f",
                    "--must-be", "below", "-w", "2", "-c", "0")

    def test_compare_unavailable(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: sensor.offline is unavailable$", "--check", "compare",
                    "--entity", "sensor.room", "--compare-entity", "sensor.offline", "--must-be", "below",
                    "-w", "2", "-c", "0")

    def test_compare_usage(self):
        for argv in (["--entity", "sensor.room", "--compare-entity", "sensor.outside", "-w", "2", "-c", "0"],
                     ["--entity", "sensor.room", "--compare-entity", "sensor.outside", "--must-be", "below",
                      "-w", "0", "-c", "2"]):
            result = self.run_plugin("--check", "compare", *argv)
            self.assertEqual(plugin.STATE_UNKNOWN, result.returncode, (argv, result))
            self.assertEqual("", result.stdout)

    # --list

    def test_list(self):
        result = self.run_plugin("--list")
        self.assertEqual(plugin.STATE_OK, result.returncode, result)
        lines = result.stdout.splitlines()
        self.assertEqual(f"Available entities on 127.0.0.1:", lines[0])
        self.assertIn("  binary_sensor:", lines)
        self.assertIn("    sensor.outside_temperature = 21.5 °C", lines)
        self.assertIn("    climate.living_room = heat", lines)

    # connection and token

    def test_token_rejected(self):
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write("wrong")
            handle.flush()
            self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: Home Assistant rejected the access token \(HTTP 401\)$",
                        "--check", "sensor", "--entity", "sensor.power", "-w", "1", "-c", "2",
                        token_file=handle.name)

    def test_token_from_environment(self):
        self.expect(plugin.STATE_OK, r"^OK: ", "--check", "text", "--entity", "sensor.firmware", "--regex",
                    "ok", token_file=False, env={"HASS_TOKEN": TOKEN})
        self.expect(plugin.STATE_OK, r"^OK: ", "--check", "text", "--entity", "sensor.firmware", "--regex",
                    "ok", token_file=False, env={"HASS_TOKEN_FILE": self.token_file})

    def test_no_token(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: No access token given", "--check", "text",
                    "--entity", "sensor.firmware", "--regex", "ok", token_file=False)

    def test_unreachable_is_critical(self):
        result = subprocess.run(
            [sys.executable, "-m", "check_homeassistant_sensor", "-H", "127.0.0.1", "-p", "1", "-T", TOKEN,
             "--check", "text", "--entity", "sensor.firmware", "--regex", "ok"],
            stdout=subprocess.PIPE, universal_newlines=True, env=dict(os.environ, PYTHONPATH=SRC), timeout=60)
        self.assertEqual(plugin.STATE_CRITICAL, result.returncode, result)
        self.assertRegex(result.stdout, r"^CRITICAL: HTTP request to http://127.0.0.1:1/api/states/"
                                        r"sensor.firmware failed: ")

    def test_usage_errors_are_unknown(self):
        for argv in (["--check", "sensor", "--entity", "sensor.power"], ["--check", "nope"], [],
                     ["--check", "text", "--entity", "x", "--regex", "("]):
            result = self.run_plugin(*argv)
            self.assertEqual(plugin.STATE_UNKNOWN, result.returncode, (argv, result))


class TestHelpers(unittest.TestCase):
    def test_parse_timestamp(self):
        self.assertEqual(1700000000.0, plugin.parse_timestamp(1700000000))
        self.assertEqual(1700000000.0, plugin.parse_timestamp("1700000000"))
        self.assertEqual(0.0, plugin.parse_timestamp("1970-01-01T00:00:00+00:00"))
        self.assertEqual(0.0, plugin.parse_timestamp("1970-01-01T00:00:00Z"))
        local = datetime(2026, 9, 29, 10, 0, 0)
        self.assertEqual(local.timestamp(), plugin.parse_timestamp("2026-09-29 10:00:00"))

    def test_extract_names(self):
        tree = plugin.parse_bool_expr("door and not switch.alarm or 'light.living-room'")
        self.assertEqual(["binary_sensor.door", "light.living-room", "switch.alarm"], plugin.extract_names(tree))

    def test_evaluate_numeric_without_trailing_separators(self):
        state, message = plugin.evaluate_numeric(5, None, None, "x")
        self.assertEqual((plugin.STATE_OK, "x is 5 | 'x'=5"), (state, message))


if __name__ == "__main__":
    unittest.main()
