"""Tests for the bash completion in completions/check_homeassistant_sensor."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "src", "check_homeassistant_sensor", "__init__.py")
COMPLETION = os.path.join(ROOT, "completions", "check_homeassistant_sensor")


def complete(*words, command="check_homeassistant_sensor", cwd=None):
    """The candidates bash offers for the last of words, which may be empty."""
    words = (command,) + words
    script = 'source "$1"; shift; COMP_WORDS=("$@"); COMP_CWORD=$(($# - 1)); ' \
             '$(complete -p "$1" | sed -E "s/.*-F ([^ ]+) .*/\\1/"); printf "%s\\n" "${COMPREPLY[@]}"'
    result = subprocess.run(["bash", "-c", script, "bash", COMPLETION] + list(words),
                            capture_output=True, text=True, check=True, cwd=cwd)
    return [line for line in result.stdout.split("\n") if line]


def help_options():
    """The option strings --help lists."""
    text = subprocess.run([sys.executable, PLUGIN, "--help"], capture_output=True, text=True,
                          check=True).stdout
    options = set()
    for line in text.split("\n"):
        match = re.match(r"\s{2}(-[^\s,]+(?:, -[^\s,]+)*)", line)
        if match:
            options.update(match.group(1).split(", "))
    return options


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class CompletionTests(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["bash", "-n", COMPLETION], check=True)

    def test_offers_exactly_the_options_of_help(self):
        self.assertEqual(set(complete("")), help_options())

    def test_option_prefix(self):
        self.assertEqual(complete("--unav"), ["--unavailable-state"])

    def test_choices(self):
        self.assertEqual(complete("--check", "a"), ["age"])
        self.assertEqual(complete("--unavailable-state", "u"), ["unknown"])
        self.assertEqual(complete("--age-of", ""), ["reported", "updated", "changed"])
        self.assertEqual(complete("--must-be", "b"), ["below"])
        for option in ("--true-state", "--false-state", "--match-state", "--no-match-state"):
            self.assertEqual(complete(option, ""), ["ok", "warning", "critical"], option)

    def test_equals_form(self):
        self.assertEqual(complete("--check", "=", "b"), ["binary"])
        self.assertEqual(complete("--age-of", "="), ["reported", "updated", "changed"])

    def test_files(self):
        with tempfile.TemporaryDirectory() as directory:
            open(os.path.join(directory, "hass.token"), "w").close()
            self.assertEqual(complete("-f", "ha", cwd=directory), ["hass.token"])
            self.assertEqual(complete("--token-file", "=", "ha", cwd=directory), ["hass.token"])
            self.assertEqual(complete("--ca-cert", "ha", cwd=directory), ["hass.token"])

    def test_no_candidates_for_values(self):
        for option in ("-w", "--critical", "--entity", "--regex", "-T", "--port"):
            self.assertEqual(complete(option, ""), [], option)


if __name__ == "__main__":
    unittest.main()
