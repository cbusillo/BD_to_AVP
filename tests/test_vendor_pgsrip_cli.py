import unittest
from typing import Any, cast

from bd_to_avp.vendor.pgsrip.cli import pgsrip


class PgsripCliTests(unittest.TestCase):
    def test_age_keeps_short_option_when_all_is_long_only(self) -> None:
        command = cast(Any, pgsrip)
        options_by_name = {option.name: option for option in command.params if hasattr(option, "opts")}

        self.assertIn("-a", options_by_name["age"].opts)
        self.assertIn("--age", options_by_name["age"].opts)
        self.assertEqual(options_by_name["all"].opts, ["--all"])


if __name__ == "__main__":
    unittest.main()
