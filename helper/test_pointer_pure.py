import unittest
from unittest import mock

import pointer_helper as h


class SecretHintTests(unittest.TestCase):
    def test_secret_looking_labels_are_caught(self):
        for label in ("Password", "Enter your passcode", "API key", "Card number", "CVV", "Security code", "Secret token", "SSN"):
            self.assertTrue(h.SECRET_HINT.search(label), label)

    def test_ordinary_labels_are_not(self):
        for label in ("Save", "Search", "Subject", "Message body", "Address"):
            self.assertFalse(h.SECRET_HINT.search(label), label)


class OrphanTests(unittest.TestCase):
    def test_a_parent_of_one_means_orphaned(self):
        with mock.patch("os.getppid", return_value=1):
            self.assertTrue(h._orphaned())

    def test_a_live_parent_with_a_live_grandparent_is_not_orphaned(self):
        run = mock.Mock(return_value=mock.Mock(stdout="4242\n"))
        with mock.patch("os.getppid", return_value=500), mock.patch("subprocess.run", run):
            self.assertFalse(h._orphaned())

    def test_a_parent_adopted_by_launchd_means_orphaned(self):
        run = mock.Mock(return_value=mock.Mock(stdout="1\n"))
        with mock.patch("os.getppid", return_value=500), mock.patch("subprocess.run", run):
            self.assertTrue(h._orphaned())

    def test_a_failed_ps_does_not_kill_the_helper(self):
        with mock.patch("os.getppid", return_value=500), mock.patch("subprocess.run", side_effect=OSError):
            self.assertFalse(h._orphaned())


class SwitchTests(unittest.TestCase):
    def test_off_clears_what_was_collected(self):
        from pathlib import Path

        w = h.Watcher()
        w.trail.append((1.0, 2.0, 3.0, False))
        w.recent.append(object())
        w.configure(False, True, Path("/x/.claude/lookat"))
        self.assertEqual(len(w.trail), 0)
        self.assertEqual(len(w.recent), 0)
        self.assertFalse(w.enabled)

    def test_the_helper_starts_off(self):
        self.assertFalse(h.Watcher().enabled)


if __name__ == "__main__":
    unittest.main()
