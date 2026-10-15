"""Regression tests for the proxy's environment-based configuration."""

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from proxy import parse_args


class ConfigurationTests(unittest.TestCase):
    GATEWAY_ENV = {"GATEWAY_IP": "127.0.0.1", "GATEWAY_PORT": "8080"}

    def parse_environment(self, environment, arguments=None):
        with patch.dict(os.environ, environment, clear=True):
            return parse_args([] if arguments is None else arguments)

    def assert_configuration_error(self, environment, arguments=None):
        errors = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            parse_args([] if arguments is None else arguments)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("error:", errors.getvalue())

    def test_gateway_ipv4_and_ipv6_are_read_and_trimmed_from_environment(self):
        cases = (
            (" \t127.0.0.1\n", " 8080\t", "127.0.0.1", 8080),
            (" 2001:0db8::1 ", " 65535\n", "2001:db8::1", 65535),
            ("::1", "1", "::1", 1),
        )
        for ip, port, expected_ip, expected_port in cases:
            with self.subTest(ip=ip, port=port):
                args = self.parse_environment(
                    {"GATEWAY_IP": ip, "GATEWAY_PORT": port}
                )
                self.assertEqual(args.ip, expected_ip)
                self.assertEqual(args.port, expected_port)

    def test_missing_or_blank_gateway_variables_are_configuration_errors(self):
        cases = (
            {},
            {"GATEWAY_IP": "127.0.0.1"},
            {"GATEWAY_PORT": "8080"},
            {"GATEWAY_IP": "", "GATEWAY_PORT": "8080"},
            {"GATEWAY_IP": " \t\n", "GATEWAY_PORT": "8080"},
            {"GATEWAY_IP": "127.0.0.1", "GATEWAY_PORT": ""},
            {"GATEWAY_IP": "127.0.0.1", "GATEWAY_PORT": " \t\n"},
        )
        for environment in cases:
            with self.subTest(environment=environment):
                self.assert_configuration_error(environment)

    def test_gateway_requires_an_ip_address(self):
        for invalid_ip in (
            "localhost",
            "gateway.example",
            "http://127.0.0.1",
            "127.0.0.999",
            "2001:db8::xyz",
            "[::1]",
        ):
            with self.subTest(ip=invalid_ip):
                self.assert_configuration_error(
                    {**self.GATEWAY_ENV, "GATEWAY_IP": invalid_ip}
                )

    def test_gateway_port_requires_an_integer_in_the_valid_range(self):
        for invalid_port in ("0", "65536", "-1", "abc", "80.0", "1e3", "80 81"):
            with self.subTest(port=invalid_port):
                self.assert_configuration_error(
                    {**self.GATEWAY_ENV, "GATEWAY_PORT": invalid_port}
                )

    def test_optional_listening_configuration_uses_defaults(self):
        args = self.parse_environment(self.GATEWAY_ENV)
        self.assertEqual(args.listen_host, "0.0.0.0")
        self.assertEqual(args.listen_port, 9000)
        self.assertEqual(args.connect_timeout, 10.0)
        self.assertFalse(args.verbose)

    def test_optional_listening_configuration_is_read_from_environment(self):
        args = self.parse_environment(
            {
                **self.GATEWAY_ENV,
                "PROXY_LISTEN_HOST": "::1",
                "PROXY_LISTEN_PORT": "12345",
            }
        )
        self.assertEqual(args.listen_host, "::1")
        self.assertEqual(args.listen_port, 12345)

    def test_invalid_listening_environment_is_rejected(self):
        cases = (
            {"PROXY_LISTEN_HOST": "hostname.example"},
            {"PROXY_LISTEN_HOST": "256.1.2.3"},
            {"PROXY_LISTEN_PORT": "zero"},
            {"PROXY_LISTEN_PORT": "0"},
            {"PROXY_LISTEN_PORT": "65536"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assert_configuration_error({**self.GATEWAY_ENV, **overrides})

    def test_explicit_listening_flags_override_even_invalid_environment_values(self):
        args = self.parse_environment(
            {
                **self.GATEWAY_ENV,
                "PROXY_LISTEN_HOST": "invalid-environment-host",
                "PROXY_LISTEN_PORT": "invalid-environment-port",
            },
            [
                "--listen-host", "127.0.0.1",
                "--listen-port", "54321",
                "--connect-timeout", "2.5",
                "--verbose",
            ],
        )
        self.assertEqual(args.listen_host, "127.0.0.1")
        self.assertEqual(args.listen_port, 54321)
        self.assertEqual(args.connect_timeout, 2.5)
        self.assertTrue(args.verbose)

    def test_each_listening_flag_overrides_only_its_matching_environment_variable(self):
        environment = {
            **self.GATEWAY_ENV,
            "PROXY_LISTEN_HOST": "::",
            "PROXY_LISTEN_PORT": "12345",
        }
        args = self.parse_environment(environment, ["--listen-host", "127.0.0.1"])
        self.assertEqual(args.listen_host, "127.0.0.1")
        self.assertEqual(args.listen_port, 12345)
        args = self.parse_environment(environment, ["--listen-port", "23456"])
        self.assertEqual(args.listen_host, "::")
        self.assertEqual(args.listen_port, 23456)

    def test_gateway_positional_arguments_are_no_longer_accepted(self):
        self.assert_configuration_error(self.GATEWAY_ENV, ["127.0.0.2", "8000"])

    def test_help_works_without_any_environment_configuration(self):
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            redirect_stdout(output),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertIn("--listen-host", output.getvalue())
        self.assertIn("--listen-port", output.getvalue())


if __name__ == "__main__":
    unittest.main()
