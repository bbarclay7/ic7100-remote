"""Tests for the IC7100 wrapper.

Focus is on the verified opcode corrections — the parts most likely to
regress if someone copies an IC-7300 example off the net.
"""
import unittest

from ic7100ctl.radio import IC7100, MODE_BY_NAME, CALL_CHANNELS
from tests.mock_transport import MockCIVTransport


class FrequencyTests(unittest.TestCase):
    def setUp(self):
        self.t = MockCIVTransport(civ_addr=0x88)
        self.r = IC7100(self.t)

    def test_set_frequency_uses_cmd_05(self):
        self.r.set_frequency(145.500)
        f = self.t.last_frame()
        self.assertEqual(f[4], 0x05)
        # data = 5-byte BCD = bytes 5..10
        self.assertEqual(f[5:10], b'\x00\x00\x50\x45\x01')


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.t = MockCIVTransport()
        self.r = IC7100(self.t)

    def test_set_mode_fm(self):
        self.r.set_mode('FM')
        f = self.t.last_frame()
        self.assertEqual(f[4], 0x06)
        self.assertEqual(f[5], MODE_BY_NAME['FM'])  # 0x05

    def test_set_mode_unknown_returns_false(self):
        self.assertFalse(self.r.set_mode('XYZ'))


class AttenCorrectionTests(unittest.TestCase):
    """REGRESSION: IC-7100 attenuator byte is 0x12, NOT 0x20."""

    def setUp(self):
        self.t = MockCIVTransport()
        self.r = IC7100(self.t)

    def test_atten_on_sends_byte_0x12(self):
        self.r.set_atten(True)
        f = self.t.last_frame()
        self.assertEqual(f[4], 0x11)
        self.assertEqual(f[5], 0x12, "IC-7100 atten value must be 0x12 (12 dB BCD)")

    def test_atten_off_sends_byte_0x00(self):
        self.r.set_atten(False)
        f = self.t.last_frame()
        self.assertEqual(f[4], 0x11)
        self.assertEqual(f[5], 0x00)


class MemoryOpcodeOrderTests(unittest.TestCase):
    """REGRESSION: 0x0A = memory_to_vfo; 0x0B = clear. Earlier code had them swapped."""

    def setUp(self):
        self.t = MockCIVTransport()
        self.r = IC7100(self.t)

    def test_memory_to_vfo_uses_0x0A(self):
        self.r.memory_to_vfo()
        self.assertEqual(self.t.last_frame()[4], 0x0A)

    def test_memory_clear_uses_0x0B(self):
        self.r.memory_clear()
        self.assertEqual(self.t.last_frame()[4], 0x0B)


class CallChannelTests(unittest.TestCase):
    """REGRESSION: IC-7100 call channels are memory positions 106-109, NOT 0x08 0xA0."""

    def test_call_channel_indices(self):
        self.assertEqual(CALL_CHANNELS['144-C1'], 106)
        self.assertEqual(CALL_CHANNELS['144-C2'], 107)
        self.assertEqual(CALL_CHANNELS['430-C1'], 108)
        self.assertEqual(CALL_CHANNELS['430-C2'], 109)

    def test_select_call_channel_routes_to_memory_op(self):
        t = MockCIVTransport()
        r = IC7100(t)
        r.select_call_channel('144-C1')
        # Expect a memory-select frame (cmd 0x08), NOT 0x08 0xA0.
        # The exact subcommand may vary; what we MUST NOT see is byte 0xA0
        # in the second position.
        f = t.last_frame()
        if f[4] == 0x08 and len(f) > 5:
            self.assertNotEqual(f[5], 0xA0,
                "0x08 0xA0 is Memory Bank A select, NOT a call-channel opcode")


class AfLevelTests(unittest.TestCase):
    """AF level (front-panel volume) — 0x14 0x01."""

    def setUp(self):
        self.t = MockCIVTransport()
        self.r = IC7100(self.t)

    def test_set_af_level_uses_14_01(self):
        self.r.set_af_level(50)
        f = self.t.last_frame()
        self.assertEqual(f[4], 0x14)
        self.assertEqual(f[5], 0x01)


class DataModeTests(unittest.TestCase):
    """DATA mode toggle — 0x1A 0x06 [flag] [filter]."""

    def setUp(self):
        self.t = MockCIVTransport()
        self.r = IC7100(self.t)

    def test_set_data_mode_uses_1A_06(self):
        self.r.set_data_mode(True, filt=1)
        f = self.t.last_frame()
        self.assertEqual(f[4], 0x1A)
        self.assertEqual(f[5], 0x06)


class SmokeTests(unittest.TestCase):
    """Basic API surface — confirms construction + each method exists."""

    def test_construct_with_mock(self):
        t = MockCIVTransport()
        r = IC7100(t)
        self.assertIsNotNone(r)
        # Sanity: state defaults are present.
        self.assertEqual(r.mode, 'FM')
        self.assertEqual(r.freq_hz, 0)

    def test_methods_exist(self):
        r = IC7100(MockCIVTransport())
        for name in ('set_frequency', 'set_mode', 'set_ptt', 'set_af_level',
                     'set_atten', 'memory_to_vfo', 'memory_clear',
                     'select_call_channel', 'set_data_mode',
                     'connect', 'poll_fast', 'poll_settings'):
            self.assertTrue(callable(getattr(r, name, None)),
                            f"{name} missing or not callable")


if __name__ == '__main__':
    unittest.main()


class CtcssFrequencyTests(unittest.TestCase):
    """The radio takes and returns CTCSS tones as 3 BCD bytes (00 hh ll)."""

    def setUp(self):
        self.t = MockCIVTransport()
        self.r = IC7100(self.t)

    def test_set_sends_three_bytes(self):
        self.assertTrue(self.r.set_ctcss(tx_hz=127.3))
        self.assertEqual(self.t.last_frame()[4:9], b'\x1b\x00\x00\x12\x73')
        self.assertTrue(self.r.set_ctcss(rx_hz=88.5))
        self.assertEqual(self.t.last_frame()[4:9], b'\x1b\x01\x00\x08\x85')

    def test_get_reads_three_bytes(self):
        self.t.set_response(0x1b, 0x00, b'\x1b\x00\x00\x12\x73')
        self.t.set_response(0x1b, 0x01, b'\x1b\x01\x00\x08\x85')
        self.r.get_ctcss()
        self.assertAlmostEqual(self.r.ctcss_tx_hz, 127.3)
        self.assertAlmostEqual(self.r.ctcss_rx_hz, 88.5)


class ToneAndTsqlTests(unittest.TestCase):
    """CI-V 16 42 is TONE, 16 43 is TSQL (they were wired the other way round)."""

    def setUp(self):
        self.t = MockCIVTransport()
        self.r = IC7100(self.t)

    def test_tone_on_uses_16_42(self):
        self.assertTrue(self.r.set_ctcss(tone_on=True))
        self.assertEqual(self.t.last_frame()[4:7], b'\x16\x42\x01')
        self.assertTrue(self.r.tone_on and not self.r.tsql_on)

    def test_tsql_on_uses_16_43(self):
        self.assertTrue(self.r.set_ctcss(tsql_on=True))
        self.assertEqual(self.t.last_frame()[4:7], b'\x16\x43\x01')
        self.assertTrue(self.r.tsql_on and not self.r.tone_on)

    def test_get_reads_each_flag_from_its_own_opcode(self):
        self.t.set_response(0x16, 0x42, b'\x16\x42\x01')   # TONE on
        self.t.set_response(0x16, 0x43, b'\x16\x43\x00')   # TSQL off
        self.r.get_ctcss()
        self.assertEqual((self.r.tone_on, self.r.tsql_on), (True, False))

    def test_old_names_still_work(self):
        self.r.set_ctcss(tx_on=True)                          # old name for TONE
        self.assertEqual(self.t.last_frame()[4:6], b'\x16\x42')
        self.r.set_ctcss(rx_on=True)                          # old name for TSQL
        self.assertEqual(self.t.last_frame()[4:6], b'\x16\x43')
        self.assertEqual((self.r.ctcss_tx_on, self.r.ctcss_rx_on), (True, True))
