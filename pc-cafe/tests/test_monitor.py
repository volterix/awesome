"""Unit tests for the agent's monitoring helpers (no display or Windows needed)."""
import os
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
import monitor as mon  # noqa: E402
import qr  # noqa: E402


class MonitorTest(unittest.TestCase):
    def test_png_encoder(self):
        png = mon.encode_png_bgra(bytes([10, 20, 30, 255]) * 6, 3, 2)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
        self.assertEqual(raw, (b"\x00" + bytes([30, 20, 10]) * 3) * 2)   # BGRA -> RGB rows

    def test_policy(self):
        policy = {"blocked_apps": ["qbittorrent", "Tor.exe"], "blocked_keywords": ["torrent", "casino"]}
        procs = [{"name": "qBittorrent.exe", "pid": 1}, {"name": "qbittorrent.exe", "pid": 2},
                 {"name": "chrome.exe", "pid": 3}]
        v = mon.check_policy(policy, {"title": "Online Casino - Google Chrome"}, procs)
        self.assertEqual([x["kind"] for x in v], ["blocked_app", "blocked_word"])
        self.assertEqual(v[0]["kill"], "qBittorrent.exe")
        self.assertEqual(mon.check_policy(policy, {"title": "F1 25"}, [{"name": "F1_25.exe", "pid": 9}]), [])
        # protected processes are never flagged for closing
        self.assertEqual(mon.check_policy({"blocked_apps": ["svchost"]}, {}, [{"name": "svchost.exe", "pid": 4}]), [])

    def test_kill_guards(self):
        self.assertIn("protected", mon.kill_process("lsass.exe", dry_run=True))
        self.assertIn("protected", mon.kill_process("pythonw.exe", dry_run=True))
        self.assertEqual(mon.kill_process("calc.exe & del *", dry_run=True), "Invalid process name")
        self.assertIsNone(mon.kill_process("qbittorrent.exe", dry_run=True))

    def test_hosts_blocking_roundtrip(self):
        base = "127.0.0.1 localhost\n# my own entry\n10.0.0.5 nas\n"
        t = mon.hosts_with_blocks(base, ["https://www.Bad-Site.com/page", "not a domain", "x.org", "x.org"])
        self.assertIn("0.0.0.0 bad-site.com", t)
        self.assertIn("0.0.0.0 www.bad-site.com", t)
        self.assertEqual(t.count("x.org\n"), 2)                # x.org + www.x.org, de-duplicated
        self.assertNotIn("not a domain", t)
        t2 = mon.hosts_with_blocks(t, ["y.net"])                 # replaces, doesn't stack
        self.assertNotIn("bad-site", t2)
        self.assertEqual(t2.count(mon.HOSTS_START), 1)
        self.assertEqual(mon.hosts_with_blocks(t2, []), base)    # removing all restores the original

    def test_apply_site_blocks_on_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix="hosts") as f:
            f.write("127.0.0.1 localhost\n")
        try:
            self.assertIsNone(mon.apply_site_blocks(["blocked.example"], path=f.name))
            with open(f.name) as fh:
                self.assertIn("0.0.0.0 blocked.example", fh.read())
        finally:
            os.unlink(f.name)

    def test_parse_process_lists(self):
        win = '"chrome.exe","1234","Console","1","123,456 K"\n"System","4","Services","0","152 K"\n'
        self.assertEqual(mon.parse_tasklist(win)[0], {"name": "chrome.exe", "pid": 1234, "mem_mb": 120.6})
        self.assertEqual(mon.parse_ps("  42  2048 bash\n")[0], {"name": "bash", "pid": 42, "mem_mb": 2.0})


class QrTest(unittest.TestCase):
    """Golden checks; the encoder was also verified module-for-module against the `qrcode`
    package (all 8 masks, versions 1-10) and decoded with OpenCV during development."""

    def test_structure(self):
        m = qr.make("hello")
        self.assertEqual(len(m), 21)                                       # version 1
        finder = [[True] * 7, [True] + [False] * 5 + [True]]
        self.assertEqual(m[0][:7], finder[0])
        self.assertEqual(m[1][:7], finder[1])
        self.assertEqual([m[6][x] for x in range(8, 13)], [True, False, True, False, True])  # timing
        self.assertTrue(m[len(m) - 8][8])                                  # always-dark module

    def test_upi_link_fits_and_is_encoded(self):
        link = qr.upi_link("cafe.name@okhdfcbank", "Cafe & Co", 120, "RIG-03 +120 min T15")
        self.assertTrue(link.startswith("upi://pay?pa=cafe.name@okhdfcbank&pn=Cafe%20%26%20Co&am=120.00&cu=INR"))
        self.assertLessEqual(len(qr.make(link)), 57)                       # version <= 10
        with self.assertRaises(ValueError):
            qr.make("x" * 400)


if __name__ == "__main__":
    unittest.main()
