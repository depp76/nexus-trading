"""tests/test_i18n.py — UI language selection (docs/ui.md 8.20)."""
import os
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

import i18n
from ui.language_switch import LanguageSwitch, FLAG_ICONS

app = QApplication.instance() or QApplication([])


class TestTr(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ko")

    def test_default_is_korean(self):
        self.assertEqual(i18n.DEFAULT_LANGUAGE, "ko")
        self.assertEqual(i18n.language(), "ko")      # the test session never loads saved settings
        self.assertEqual(i18n.tr("닫기", "Close"), "닫기")

    def test_english(self):
        i18n.set_language("en")
        self.assertEqual(i18n.tr("닫기", "Close"), "Close")

    def test_unknown_language_falls_back_to_korean(self):
        i18n.set_language("fr")
        self.assertEqual(i18n.language(), "ko")

    def test_save_rejects_unknown_language(self):
        with self.assertRaises(ValueError):
            i18n.save_language("fr")

    def test_saved_language_ignores_garbage(self):
        class _S:
            def value(self, *_a):
                return "xx"
        with patch.object(i18n, "_settings", return_value=_S()):
            self.assertEqual(i18n.saved_language(), "ko")


class TestLanguageSwitch(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ko")

    def test_flag_icons_exist(self):
        for path in FLAG_ICONS.values():
            self.assertTrue(os.path.isfile(path), path)

    def test_current_language_is_checked(self):
        sw = LanguageSwitch()
        self.assertEqual(list(sw.buttons), ["ko", "en"])
        self.assertTrue(sw.buttons["ko"].isChecked())
        self.assertFalse(sw.buttons["en"].isChecked())
        self.assertFalse(sw.buttons["ko"].icon().isNull())
        self.assertEqual(sw.buttons["en"].toolTip(), "English")
        self.assertEqual(sw.buttons["en"].text(), "")      # flag only, no label (8.1)
        i18n.set_language("en")
        self.assertTrue(LanguageSwitch().buttons["en"].isChecked())

    def test_other_flag_requests_a_switch_and_reset_restores(self):
        sw = LanguageSwitch()
        got = []
        sw.language_requested.connect(got.append)
        sw.buttons["ko"].click()                 # current language: no request
        self.assertEqual(got, [])
        sw.buttons["en"].click()
        self.assertEqual(got, ["en"])
        self.assertTrue(sw.buttons["en"].isChecked())
        sw.reset()                               # switch declined
        self.assertTrue(sw.buttons["ko"].isChecked())


if __name__ == "__main__":
    unittest.main()
