"""Tests for the CI-V transport: framing, BCD helpers, fairness flags."""
import threading
import unittest

from ic7100ctl.civ import (
    CIVTransport, PREAMBLE, END, CTRL_ADDR,
    freq_to_bcd, bcd_to_freq,
    tone_to_bcd, tone_to_bcd3, bcd_to_tone,
    rit_offset_to_bcd, bcd_to_rit_offset,
    pct_to_bcd3, bcd3_to_value,
)


class FrequencyBcdTests(unittest.TestCase):
    """5-byte BCD frequency round-trip, LSB-first."""

    def test_round_trip_known_values(self):
        for hz in (1, 1_000, 14_250_000, 145_500_000, 433_500_000, 9_999_999_999):
            with self.subTest(hz=hz):
                self.assertEqual(bcd_to_freq(freq_to_bcd(hz)), hz)

    def test_length_is_5_bytes(self):
        for hz in (0, 145_500_000, 9_999_999_999):
            self.assertEqual(len(freq_to_bcd(hz)), 5)

    def test_known_encoding_145m500(self):
        # 145.500.000 → digits LSB-first: 0,0,0,0,0,5,5,4,1,0
        # Packed (lo|hi<<4) per pair: 0x00 0x00 0x50 0x45 0x01
        self.assertEqual(freq_to_bcd(145_500_000), b'\x00\x00\x50\x45\x01')


class ToneBcdTests(unittest.TestCase):
    def test_round_trip(self):
        for hz in (67.0, 88.5, 100.0, 254.1):
            with self.subTest(hz=hz):
                self.assertAlmostEqual(bcd_to_tone(tone_to_bcd(hz)), hz, places=1)

    def test_88_5_encoding(self):
        # 88.5 Hz → tenths = 885 → bytes: 0x08, 0x85
        self.assertEqual(tone_to_bcd(88.5), b'\x08\x85')


class RitOffsetBcdTests(unittest.TestCase):
    def test_zero(self):
        b = rit_offset_to_bcd(0)
        self.assertEqual(bcd_to_rit_offset(b), 0)

    def test_positive_round_trip(self):
        for hz in (1, 50, 500, 9999):
            with self.subTest(hz=hz):
                self.assertEqual(bcd_to_rit_offset(rit_offset_to_bcd(hz)), hz)

    def test_negative_round_trip(self):
        for hz in (-1, -50, -500, -9999):
            with self.subTest(hz=hz):
                self.assertEqual(bcd_to_rit_offset(rit_offset_to_bcd(hz)), hz)


class PercentBcdTests(unittest.TestCase):
    def test_round_trip(self):
        for v in (0, 1, 50, 100, 128, 255):
            with self.subTest(v=v):
                self.assertEqual(bcd3_to_value(pct_to_bcd3(v)), v)


class FrameBuildingTests(unittest.TestCase):
    def setUp(self):
        # Construct WITHOUT calling connect() — transport is offline.
        self.t = CIVTransport(port='/dev/null', baud=19200, civ_addr=0x88)

    def test_frame_layout(self):
        frame = self.t.build_frame(0x05, data=b'\x00\x00\x50\x45\x01')
        # FE FE 88 E0 05 00 00 50 45 01 FD
        self.assertTrue(frame.startswith(PREAMBLE))
        self.assertTrue(frame.endswith(END))
        self.assertEqual(frame[2], 0x88)            # radio addr
        self.assertEqual(frame[3], CTRL_ADDR)        # controller addr
        self.assertEqual(frame[4], 0x05)             # cmd

    def test_frame_with_subcmd(self):
        frame = self.t.build_frame(0x14, 0x01, data=b'\x05\x00')
        # FE FE 88 E0 14 01 05 00 FD
        self.assertEqual(frame[4], 0x14)
        self.assertEqual(frame[5], 0x01)
        self.assertEqual(frame[6:8], b'\x05\x00')
        self.assertEqual(frame[-1:], END)

    def test_atten_opcode_is_12_not_20(self):
        """Regression: IC-7100 attenuator value is 0x12, not 0x20.

        See docs/protocol.md "Corrections vs other implementations".
        """
        frame = self.t.build_frame(0x11, data=b'\x12')
        self.assertEqual(frame[4], 0x11)
        self.assertEqual(frame[5], 0x12)


class FairnessFlagsTests(unittest.TestCase):
    def test_user_cmd_pending_is_threading_event(self):
        t = CIVTransport(port='/dev/null', baud=19200, civ_addr=0x88)
        self.assertIsInstance(t.user_cmd_pending, threading.Event)
        self.assertFalse(t.user_cmd_pending.is_set())
        t.user_cmd_pending.set()
        self.assertTrue(t.user_cmd_pending.is_set())

    def test_mark_poll_thread_sets_tls_flag(self):
        t = CIVTransport(port='/dev/null', baud=19200, civ_addr=0x88)
        self.assertFalse(getattr(t._tls, 'in_poll', False))
        t.mark_poll_thread()
        self.assertTrue(t._tls.in_poll)


if __name__ == '__main__':
    unittest.main()


class ToneThreeByteTests(unittest.TestCase):
    def test_tone_to_bcd3_has_a_leading_zero_byte(self):
        self.assertEqual(tone_to_bcd3(127.3), b'\x00\x12\x73')
        self.assertEqual(tone_to_bcd3(88.5), b'\x00\x08\x85')
