import unittest

from bot.utils.maps import format_monitor_map_name, normalize_map_key


class MonitorMapFormattingTests(unittest.TestCase):
    def test_regular_map_keeps_human_format(self):
        self.assertEqual(format_monitor_map_name("de_mirage"), "Mirage")

    def test_workshop_cache_path_uses_de_cache(self):
        self.assertEqual(
            format_monitor_map_name("workshop/3328271311/cache"),
            "de_cache",
        )

    def test_workshop_cobble_aliases_use_de_cobblestone(self):
        self.assertEqual(format_monitor_map_name("cble"), "de_cobblestone")
        self.assertEqual(format_monitor_map_name("de_cbble_d"), "de_cobblestone")

    def test_workshop_cache_uses_cache_thumbnail_key(self):
        self.assertEqual(normalize_map_key("workshop/3328271311/cache"), "cache")

    def test_workshop_cobble_uses_cobblestone_thumbnail_key(self):
        self.assertEqual(normalize_map_key("de_cbble_d"), "cobblestone")


if __name__ == "__main__":
    unittest.main()
