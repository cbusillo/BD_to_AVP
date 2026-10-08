import re
import runpy
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from babelfish import Error as BabelfishError, Language
from cleanit import config as cleanit_config
from click.testing import CliRunner
import click

from bd_to_avp.vendor.pgsrip.cli import pgsrip
from bd_to_avp.vendor.pgsrip.media import Pgs
from bd_to_avp.vendor.pgsrip.options import Options
from bd_to_avp.vendor.pgsrip.sup import Sup


class PgsripCliTests(unittest.TestCase):
    def setUp(self) -> None:
        packaged_rules = cleanit_config.merge_options(*cleanit_config.load_default_resources())
        self.enterContext(patch.object(cleanit_config, "default_config", packaged_rules))

    def test_invalid_config_fails_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for config_path in (root / "missing.conf", root):
                with self.subTest(config=config_path.name), patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan:
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), str(root / "movie.sup")])

                    self.assertEqual(result.exit_code, 2, result.output)
                    scan.assert_not_called()

    def test_defaults_scan_multiple_paths_and_rip_collected_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("first.en.sup", "second.fr.sup")]
            for path in paths:
                path.write_bytes(b"extraction boundary")
            subtitles = [Mock(spec=Pgs), Mock(spec=Pgs)]
            with (
                patch.object(Sup, "get_pgs_medias", side_effect=[[subtitles[0]], [subtitles[1]]]) as collect,
                patch("bd_to_avp.vendor.pgsrip.cli.api.rip_pgs", side_effect=[True, False]) as rip,
            ):
                result = CliRunner().invoke(pgsrip, [str(path) for path in paths])

            self.assertEqual(result.exit_code, 0, result.exception)
            self.assertEqual(collect.call_count, len(paths))
            self.assertEqual([call.args[0] for call in rip.call_args_list], subtitles)
            options = rip.call_args.args[1]
            self.assertEqual(options.languages, set())
            self.assertTrue(options.one_per_lang)
            self.assertFalse(options.overwrite)
            self.assertFalse(options.keep_temp_files)
            self.assertIsNone(options.age)
            self.assertIsNone(options.srt_age)
            self.assertIsNone(options.max_workers)
            self.assertIn("1 PGS subtitle ripped from 2 files", result.output)

    def test_existing_config_reaches_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            tag = "cli-config-test"
            config_path.write_text(
                json.dumps({"rules": {"cli-test": {"tags": [tag], "patterns": ["typo"], "replacement": "corrected"}}}),
                encoding="utf-8",
            )
            with patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path", return_value=([], [], [])) as scan:
                result = CliRunner().invoke(
                    pgsrip, ["--config", str(config_path), "--tag", tag, str(Path(directory) / "movie.sup")]
                )

            self.assertEqual(result.exit_code, 0, result.exception)
            scan.assert_called_once()
            options = scan.call_args.args[1]
            selected = options.config.select_rules(tags=options.tags, languages=options.languages)
            self.assertEqual([rule.name for rule in selected], ["cli-test"])
            self.assertEqual(selected.apply("typo"), ("corrected", True))

    def test_partial_custom_rule_inherits_patterns_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.yaml"
            config_path.write_text("rules:\n  inherited:\n    replacement: corrected\n", encoding="utf-8")
            defaults = {
                "aliases": {"TOKEN": "typo"},
                "rules": {"inherited": {"patterns": "TOKEN", "tags": ["default"], "replacement": "old"}},
            }
            with patch.object(cleanit_config, "default_config", defaults):
                options = Options(config_path=str(config_path))

            self.assertEqual(options.config.select_rules(tags=options.tags).apply("typo"), ("corrected", True))
            self.assertEqual(defaults["rules"]["inherited"]["replacement"], "old")

    def test_custom_override_can_repair_a_broken_default_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            config_path.write_text(json.dumps({"rules": {"inherited": {"patterns": "typo"}}}), encoding="utf-8")
            defaults = {"rules": {"inherited": {"patterns": "(", "tags": ["default"]}}}
            with patch.object(cleanit_config, "default_config", defaults):
                options = Options(config_path=str(config_path))

            self.assertEqual(options.config.select_rules(tags=options.tags).apply("typo"), (None, True))

    def test_custom_alias_failure_in_inherited_rule_names_that_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            config_path.write_text(json.dumps({"aliases": {"TOKEN": "("}}), encoding="utf-8")
            defaults = {"aliases": {"TOKEN": "typo"}, "rules": {"inherited": {"patterns": "TOKEN"}}}
            with (
                patch.object(cleanit_config, "default_config", defaults),
                patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan,
            ):
                result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])

            self.assertEqual(result.exit_code, 2, result.exception)
            self.assertIn("inherited", result.output)
            scan.assert_not_called()

    def test_unexpected_rule_constructor_errors_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            config_path.write_text("{}", encoding="utf-8")
            for error in (TypeError("constructor bug"), ValueError("constructor bug"), RuntimeError("constructor bug")):
                with (
                    self.subTest(error=error),
                    patch("bd_to_avp.vendor.pgsrip.options.Rule", side_effect=error),
                    patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan,
                ):
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])

                self.assertEqual(result.exit_code, 1)
                self.assertIs(result.exception, error)
                scan.assert_not_called()

    def test_yaml_scalar_mapping_keys_are_rejected_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.yaml"
            for key in ("10", "true", "null", "1.5", "2026-10-08"):
                for content in (
                    f"{key}: ignored\n",
                    f"aliases:\n  {key}: typo\n",
                    f"rules:\n  {key}: {{patterns: typo}}\n",
                    f"rules:\n  custom: {{patterns: typo, examples: {{{key}: null}}}}\n",
                    f"templates:\n  - patterns: typo\n    examples: {{{key}: null}}\n",
                ):
                    with (
                        self.subTest(key=key, content=content),
                        patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan,
                    ):
                        config_path.write_text(content, encoding="utf-8")
                        result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])
                        self.assertEqual(result.exit_code, 2, result.exception)
                        self.assertIn(str(config_path), result.output)
                        scan.assert_not_called()

    def test_malformed_replacement_templates_are_rejected_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            for replacement in (r"\2", r"\g<missing>", r"\q", "\\"):
                with self.subTest(replacement=replacement), patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan:
                    config_path.write_text(
                        json.dumps({"rules": {"custom": {"patterns": "l(o)ve", "replacement": replacement}}}),
                        encoding="utf-8",
                    )
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])
                    self.assertEqual(result.exit_code, 2, result.exception)
                    self.assertIn("custom", result.output)
                    self.assertIn(str(config_path), result.output)
                    scan.assert_not_called()

    def test_custom_tag_filter_does_not_preflight_an_inherited_bad_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            defaults = {"rules": {"inherited": {"patterns": "love", "replacement": r"\g<missing>"}}}
            config_path.write_text(json.dumps({"rules": {"inherited": {"tags": "unused"}}}), encoding="utf-8")
            with patch.object(cleanit_config, "default_config", defaults):
                options = Options(config_path=str(config_path))
            self.assertEqual(list(options.config.select_rules(tags={"default"})), [])

    def test_repaired_default_pattern_does_not_hide_custom_template_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            config_path.write_text(
                json.dumps({"rules": {"inherited": {"patterns": "love", "replacement": r"\g<missing>"}}}),
                encoding="utf-8",
            )
            for original in (
                {"patterns": "(", "replacement": r"\g<missing>"},
                {"replacement": r"\g<missing>"},
                {"patterns": "love", "languages": "zz-bogus"},
            ):
                with (
                    self.subTest(original=original),
                    patch.object(cleanit_config, "default_config", {"rules": {"inherited": original}}),
                    patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan,
                ):
                    # Repair the language as well when it came from defaults.
                    config_path.write_text(
                        json.dumps(
                            {
                                "rules": {
                                    "inherited": {"patterns": "love", "replacement": r"\g<missing>", "languages": "en"}
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])
                    self.assertEqual(result.exit_code, 2, result.exception)
                    self.assertIn("inherited", result.output)
                    scan.assert_not_called()

    def test_custom_config_can_disable_a_broken_default_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            defaults = {"rules": {"inherited": {"patterns": "love", "replacement": r"\g<missing>"}}}
            config_path.write_text(json.dumps({"rules": {"inherited": {"disabled": True}}}), encoding="utf-8")
            with patch.object(cleanit_config, "default_config", defaults):
                options = Options(config_path=str(config_path))
            self.assertEqual(list(options.config.select_rules(tags={"all"})), [])

    def test_repaired_default_template_does_not_hide_custom_language_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            defaults = {"rules": {"inherited": {"patterns": "love", "replacement": r"\g<missing>"}}}
            config_path.write_text(
                json.dumps({"rules": {"inherited": {"replacement": "corrected", "languages": "zz-bogus"}}}),
                encoding="utf-8",
            )
            with (
                patch.object(cleanit_config, "default_config", defaults),
                patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan,
            ):
                result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])
            self.assertEqual(result.exit_code, 2, result.exception)
            self.assertIn("inherited", result.output)
            scan.assert_not_called()

    def test_untouched_unused_default_templates_do_not_block_custom_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            defaults = {"rules": {"unused": {"patterns": "l(o)ve", "replacement": r"\2", "tags": "unused"}}}
            for custom in ({}, {"aliases": {"UNRELATED": "typo"}}):
                with (
                    self.subTest(custom=custom),
                    patch.object(cleanit_config, "default_config", defaults),
                    patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path", return_value=([], [], [])) as scan,
                ):
                    config_path.write_text(json.dumps(custom), encoding="utf-8")
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "--tag", "all", "movie.sup"])
                    self.assertEqual(result.exit_code, 0, result.exception)
                    scan.assert_called_once()
                    selected = scan.call_args.args[1].config.select_rules(tags={"all"})
                    with self.assertRaises(re.error):
                        selected.apply("love")

    def test_replacement_checks_use_every_merged_pattern_and_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            defaults = {
                "aliases": {"TOKEN": "l(o)ve"},
                "rules": {"inherited": {"patterns": "TOKEN", "replacement": r"ha\1te", "tags": "trial"}},
            }
            for override in (
                {"aliases": {"TOKEN": "love"}},
                {"rules": {"inherited": {"patterns": ["l(o)ve", "love"]}}},
            ):
                with (
                    self.subTest(override=override),
                    patch.object(cleanit_config, "default_config", defaults),
                    patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan,
                ):
                    config_path.write_text(json.dumps(override), encoding="utf-8")
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])
                    self.assertEqual(result.exit_code, 2, result.exception)
                    self.assertIn("inherited", result.output)
                    scan.assert_not_called()

    def test_valid_yaml_keys_and_replacements_clean_real_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.yaml"
            config_path.write_text(
                "rules:\n  '10':\n    patterns: 'l(?P<middle>o)ve'\n"
                "    tags: trial\n    replacement: 'ha\\g<middle>te'\n"
                "    examples: {'true': null}\n",
                encoding="utf-8",
            )
            options = Options(config_path=str(config_path), tags={"trial"})
            self.assertEqual(options.config.select_rules(tags=options.tags).apply("love"), ("haote", True))

    def test_inherited_replacement_attribution_and_custom_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            for replacement, exception in ((r"\2", re.error), (r"\g<missing>", IndexError)):
                defaults = {"rules": {"inherited": {"patterns": "l(o)ve", "replacement": replacement}}}
                config_path.write_text(
                    json.dumps({"rules": {"inherited": {"patterns": "l(o)ve!", "tags": "trial"}}}), encoding="utf-8"
                )
                with self.subTest(replacement=replacement), patch.object(cleanit_config, "default_config", defaults):
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])
                    self.assertEqual(result.exit_code, 1)
                    self.assertIsInstance(result.exception, exception)
                    with self.assertRaises(exception):
                        Options().config.select_rules(tags={"all"}).apply("love")
                    config_path.write_text(
                        json.dumps({"rules": {"inherited": {"tags": "trial", "replacement": r"ha\1te"}}}),
                        encoding="utf-8",
                    )
                    options = Options(config_path=str(config_path), tags={"trial"})
                    self.assertEqual(options.config.select_rules(tags=options.tags).apply("love"), ("haote", True))

    def test_invalid_config_content_is_a_usage_error_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in (
                ("broken.json", b"{bad"),
                ("broken.yaml", b"rules: ["),
                ("invalid.json", b'{"rules": []}'),
                ("encoding.json", b"\xff"),
            ):
                with self.subTest(name=name), patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan:
                    config_path = root / name
                    config_path.write_bytes(content)
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), str(root / "movie.sup")])

                    self.assertEqual(result.exit_code, 2, result.output)
                    self.assertIn("--config", result.output)
                    scan.assert_not_called()

    def test_config_open_failure_is_a_usage_error_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            config_path.write_text("{}", encoding="utf-8")
            # The file passes Click's access check, then loses read permission.
            with (
                patch(
                    "bd_to_avp.vendor.pgsrip.options.open", side_effect=PermissionError("read permission changed")
                ) as open_config,
                patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan,
            ):
                result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])

            open_config.assert_called_once_with(str(config_path))
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertIn("--config", result.output)
            scan.assert_not_called()

    def test_invalid_custom_rules_are_usage_errors_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            for rule in (
                {"patterns": "("},
                {},
                {"patterns": "foo", "flags": "locale"},
                {"patterns": r"\d{9999999999}"},
                {"patterns": "(" * sys.getrecursionlimit() + "x" + ")" * sys.getrecursionlimit()},
                *({"patterns": "typo", "languages": language} for language in ("zz-bogus", "en-UK", "xyz", "pt-BRA")),
            ):
                with (
                    self.subTest(rule=rule),
                    patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path", return_value=([], [], [])) as scan,
                ):
                    config_path.write_text(json.dumps({"rules": {"custom-rule": rule}}), encoding="utf-8")
                    result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])

                    self.assertEqual(result.exit_code, 2, result.exception)
                    self.assertIn("--config", result.output)
                    self.assertIn("custom-rule", result.output)
                    scan.assert_not_called()

    def test_fifo_configuration_is_rejected_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            os.mkfifo(config_path)
            with patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path", return_value=([], [], [])) as scan:
                result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])

            self.assertEqual(result.exit_code, 2, result.exception)
            self.assertIn("--config", result.output)
            scan.assert_not_called()

    def test_file_removed_after_click_conversion_is_rejected_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            config_path.write_text("{}", encoding="utf-8")
            convert = click.Path.convert

            def remove_after_conversion(path_type, value, param, ctx):
                converted = convert(path_type, value, param, ctx)
                if param.name == "config":
                    config_path.unlink()
                return converted

            with (
                patch.object(click.Path, "convert", remove_after_conversion),
                patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path", return_value=([], [], [])) as scan,
            ):
                result = CliRunner().invoke(pgsrip, ["--config", str(config_path), "movie.sup"])

            self.assertEqual(result.exit_code, 2, result.exception)
            self.assertIn("--config", result.output)
            scan.assert_not_called()

    def test_host_rule_errors_are_not_attributed_to_custom_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            for host_rule, error_type in (
                ({"patterns": "("}, re.error),
                ({}, TypeError),
                ({"patterns": "typo", "languages": "zz-bogus"}, BabelfishError),
                ({"patterns": "typo", "languages": "en-UK"}, ValueError),
                ({"patterns": "foo", "flags": "locale"}, ValueError),
                ({"patterns": r"\d{9999999999}"}, OverflowError),
                ({"patterns": "(" * sys.getrecursionlimit() + "x" + ")" * sys.getrecursionlimit()}, RecursionError),
            ):
                for custom_rules in ({}, {"host-rule": {"tags": ["custom"]}}):
                    for arguments in ([], ["--config", str(config_path)]):
                        with (
                            self.subTest(host_rule=host_rule, custom_rules=custom_rules, arguments=arguments),
                            patch.object(cleanit_config, "default_config", {"rules": {"host-rule": host_rule}}),
                            patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan,
                        ):
                            config_path.write_text(json.dumps({"rules": custom_rules}) if custom_rules else "{}")
                            result = CliRunner().invoke(pgsrip, [*arguments, "movie.sup"])

                        self.assertEqual(result.exit_code, 1, result.output)
                        self.assertIsInstance(result.exception, error_type)
                        self.assertNotIn("Invalid value for '--config'", result.output)
                        scan.assert_not_called()

    def test_unexpected_configuration_errors_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "cleanit.json"
            config_path.write_text("{}", encoding="utf-8")
            for arguments in ([], ["--config", str(config_path)]):
                with (
                    self.subTest(arguments=arguments),
                    patch(
                        "bd_to_avp.vendor.pgsrip.options.validate"
                        if arguments
                        else "bd_to_avp.vendor.pgsrip.options.Config",
                        side_effect=RuntimeError("unexpected configuration failure"),
                    ),
                ):
                    result = CliRunner().invoke(pgsrip, [*arguments, "movie.sup"])

                self.assertEqual(result.exit_code, 1)
                self.assertIsInstance(result.exception, RuntimeError)

    def test_age_all_and_repeated_options_reach_extraction_in_each_progress_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.en.sup"
            source.write_bytes(b"extraction boundary")
            for verbosity in ([], ["-vv"], ["--debug"]):
                with self.subTest(verbosity=verbosity):
                    subtitle = Mock(spec=Pgs)
                    with (
                        patch.object(Sup, "get_pgs_medias", return_value=[subtitle]),
                        patch("bd_to_avp.vendor.pgsrip.cli.api.rip_pgs", return_value=True) as rip,
                        patch("bd_to_avp.vendor.pgsrip.cli.logger"),
                    ):
                        result = CliRunner().invoke(
                            pgsrip,
                            [
                                "-a",
                                "1w2d3h",
                                "-A",
                                "12h",
                                "--all",
                                "--force",
                                "--keep-temp-files",
                                "--max-workers",
                                "2",
                                "--encoding",
                                "utf-8",
                                "-l",
                                "en",
                                "-l",
                                "fr",
                                "-t",
                                "default",
                                "-t",
                                "ocr",
                                *verbosity,
                                str(source),
                            ],
                        )

                    self.assertEqual(result.exit_code, 0, result.exception)
                    rip.assert_called_once()
                    self.assertIs(rip.call_args.args[0], subtitle)
                    options = rip.call_args.args[1]
                    self.assertEqual(options.languages, {Language.fromietf("en"), Language.fromietf("fr")})
                    self.assertEqual(options.tags, {"default", "ocr"})
                    self.assertEqual(options.age, timedelta(weeks=1, days=2, hours=3))
                    self.assertEqual(options.srt_age, timedelta(hours=12))
                    self.assertFalse(options.one_per_lang)
                    self.assertTrue(options.overwrite)
                    self.assertTrue(options.keep_temp_files)
                    self.assertEqual(options.max_workers, 2)
                    self.assertEqual(options.encoding, "utf-8")
                    self.assertIn("1 PGS subtitle ripped", result.output)

    def test_invalid_parameters_are_usage_errors_before_scanning(self) -> None:
        for arguments in (
            ["--age", "yesterday"],
            ["--srt-age", "1h2d"],
            ["--language", "not-a-language"],
            ["--max-workers", "0"],
            ["--max-workers", "51"],
        ):
            with self.subTest(arguments=arguments), patch("bd_to_avp.vendor.pgsrip.cli.api.scan_path") as scan:
                result = CliRunner().invoke(pgsrip, [*arguments, "movie.sup"])

                self.assertEqual(result.exit_code, 2, result.output)
                scan.assert_not_called()

    def test_module_entrypoint_dispatches_click_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.en.sup"
            source.write_bytes(b"extraction boundary")
            with (
                patch.object(sys, "argv", ["pgsrip", "--all", str(source)]),
                patch.object(Sup, "get_pgs_medias", return_value=[Mock(spec=Pgs)]),
                patch("bd_to_avp.vendor.pgsrip.cli.api.rip_pgs", return_value=True) as rip,
                self.assertRaises(SystemExit) as exit_result,
            ):
                runpy.run_module("bd_to_avp.vendor.pgsrip", run_name="__main__")

            self.assertEqual(exit_result.exception.code, 0)
            rip.assert_called_once()
            self.assertFalse(rip.call_args.args[1].one_per_lang)


if __name__ == "__main__":
    unittest.main()
