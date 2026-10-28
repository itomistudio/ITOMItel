"""Offline tests for the serial backend: python3 -m unittest discover tests"""
import unittest

from totem.app import (ANNUL, CORREZIONE, DESTRA, INDICE, INVIO, SEGUENTE, SINISTRA, App, render)
from totem.serial_cli import ingest
from totem.terminal import (KeyDecoder, SerialTerminal, encode_screen,
                            estimate_seconds, to_ascii)


class FakeShop:
    """Just enough of Shop for the Home screen."""
    def collections(self):
        return []

    def refresh(self):
        pass


class FakePort:
    def __init__(self):
        self.out = b""

    def write(self, b):
        self.out += b


def screen(*lines, w=40):
    return render(w, "TITOLO", list(lines), hint="hint", keys="keys")


class EncodeTests(unittest.TestCase):
    def test_to_ascii_spells_out_accents_and_euro(self):
        self.assertEqual(to_ascii("Novità 4,99€ è"), "Novita' 4,99EUR e'")

    def test_plain_trims_trailing_spaces_and_has_24_rows(self):
        data = encode_screen(screen("a"), None, "plain", margin=0)
        self.assertEqual(data.count(b"\r\n"), 24)  # leading CRLF + 23 separators
        self.assertNotIn(b"  \r\n", data)

    def test_ansi_diff_sends_only_changed_rows(self):
        a, b = screen("uno", "due"), screen("uno", "tre")
        full = encode_screen(b, None, "ansi", margin=0)
        diff = encode_screen(b, a, "ansi", margin=0)
        self.assertLess(len(diff), len(full))
        self.assertIn(b"tre", diff)
        self.assertNotIn(b"uno", diff)

    def test_identical_screens_send_only_cursor_park(self):
        a = screen("uno")
        self.assertEqual(encode_screen(a, a, "ansi", 0), b"\x1b[24;40H")

    def test_cursor_parks_in_the_last_column_of_an_80_column_screen(self):
        a, b = screen("uno", w=80), screen("due", w=80)
        self.assertTrue(encode_screen(b, a, "ansi", 0).endswith(b"\x1b[24;80H"))

    def test_margin_centres_text(self):
        data = encode_screen(screen("x"), None, "ff", margin=20)
        self.assertIn(b" " * 20 + b"TITOLO", data)

    def test_full_redraw_estimate_is_seconds_not_milliseconds(self):
        data = encode_screen(screen("x" * 40, "y" * 40), None, "ff", margin=20)
        self.assertGreater(estimate_seconds(data, 1200), 2)

    def test_terminal_remembers_previous_screen(self):
        port = FakePort()
        t = SerialTerminal(port, "ansi", margin=0)
        t.draw(screen("uno", "due"))
        first = len(port.out)
        t.draw(screen("uno", "due"))
        self.assertLess(len(port.out) - first, 20)


class FailingPort:
    """A port whose write times out, like the HCS 80 stuck at boot."""
    def write(self, b):
        raise TimeoutError("Write timeout")


class RobustnessTests(unittest.TestCase):
    def test_try_draw_survives_a_failing_port_and_forces_a_full_redraw_next(self):
        t = SerialTerminal(FailingPort(), "ansi")
        t.prev = screen("vecchio", w=80)
        self.assertEqual(t.try_draw(screen("nuovo", w=80)), 0.0)   # no exception
        self.assertIsNone(t.prev)

    def test_draw_itself_still_raises_so_tools_can_report_the_error(self):
        with self.assertRaises(TimeoutError):
            SerialTerminal(FailingPort(), "ansi").draw(screen("x"))


class DecoderTests(unittest.TestCase):
    def test_printable_chars_and_defaults(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"ab\r\x08\t"), ["a", "b", INVIO, CORREZIONE, SEGUENTE])

    def test_learned_multibyte_sequence(self):
        d = KeyDecoder({INDICE: [b"\x1b[H"]})
        self.assertEqual(d.feed(b"\x1b["), [])          # waits for more
        self.assertEqual(d.feed(b"H"), [INDICE])

    def test_lone_prefix_resolves_on_flush(self):
        d = KeyDecoder({ANNUL: [b"\x1b"], INDICE: [b"\x1b[H"]})
        self.assertEqual(d.feed(b"\x1b"), [])
        self.assertEqual(d.flush(), [ANNUL])

    def test_unknown_control_bytes_are_dropped(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x01a\x02"), ["a"])

    def test_text_before_sequence(self):
        d = KeyDecoder({INDICE: [b"\x1b[H"]})
        self.assertEqual(d.feed(b"x\x1b[H"), ["x", INDICE])


class ArrowKeyTests(unittest.TestCase):
    def test_an_unknown_escape_sequence_is_dropped_whole_and_remembered(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"a\x1b[Ab"), ["a", "b"])        # up arrow: no stray "[" and "A" typed
        self.assertEqual(d.take_unknown(), [b"\x1b[A"])
        self.assertEqual(d.take_unknown(), [])                    # reported once

    def test_other_escape_forms(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1bOA\x1b[1;5D\x1bxz"), ["z"])
        self.assertEqual(d.take_unknown(), [b"\x1bOA", b"\x1b[1;5D", b"\x1bx"])

    def test_a_sequence_split_in_two_reads_is_put_together(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b"), [])
        self.assertEqual(d.feed(b"[B"), [])
        self.assertEqual(d.take_unknown(), [b"\x1b[B"])
        self.assertEqual(d.feed(b"\x1b"), [])                    # a known one, split the same way
        self.assertEqual(d.feed(b"[D"), [SINISTRA])

    def test_a_lone_escape_is_dropped_after_a_pause(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b"), [])
        self.assertEqual(d.flush(), [])
        self.assertEqual(d.take_unknown(), [b"\x1b"])

    def test_learned_arrows_become_their_own_keys(self):
        d = KeyDecoder({DESTRA: [b"\x1b[C"], SINISTRA: [b"\x1b[D"]})
        self.assertEqual(d.feed(b"\x1b[C\x1b[Dx"), [DESTRA, SINISTRA, "x"])
        self.assertEqual(d.take_unknown(), [])

    def test_the_arrows_of_the_hcs_80_work_without_any_learning(self):
        d = KeyDecoder()                                          # no keymap.json at all
        self.assertEqual(d.feed(b"\x1b[C\x1b[D"), [DESTRA, SINISTRA])
        self.assertEqual(d.take_unknown(), [])
        d.feed(b"\x1b[A\x1b[B")                                  # up and down: seen, ignored, logged
        self.assertEqual(d.take_unknown(), [b"\x1b[A", b"\x1b[B"])

    def test_a_learned_keymap_still_wins_over_the_defaults(self):
        d = KeyDecoder({DESTRA: [b"\x1bOC"]})
        self.assertEqual(d.feed(b"\x1bOC"), [DESTRA])

    def test_arrows_do_what_seguente_and_precedente_do(self):
        class Shop:
            def collections(self): return []
            def refresh(self): pass
        app = App(Shop())
        app.key("0")                                             # Credits: two pages
        self.assertIn("1/2", app.screen()[0])
        app.key(DESTRA)
        self.assertIn("2/2", app.screen()[0])
        app.key(SINISTRA)
        self.assertIn("1/2", app.screen()[0])
        app.key(SINISTRA)                                        # first page: like Precedente, back to the menu
        self.assertEqual(app.screen()[0], "ITOMItel".center(80))

    def test_arrows_are_harmless_where_seguente_is(self):
        class Shop:
            def collections(self): return []
            def refresh(self): pass
        app = App(Shop())
        before = app.screen()
        app.key(DESTRA)
        self.assertEqual(app.screen(), before)                  # the menu has no pages
        app.key("5")                                             # search: typing is untouched
        app.key("a"); app.key(DESTRA); app.key("b")
        self.assertIn("> ab_", "\n".join(app.screen()))


class IngestTests(unittest.TestCase):
    """The main-loop decision between feed() and flush(). Flushing on every empty read
    cut multi-byte keys in half when their first byte reached pyserial on its own."""

    def test_bytes_feed_the_decoder_and_mark_the_last_byte_time(self):
        d = KeyDecoder()
        keys, last = ingest(d, b"a", now=10.0, last_byte=9.9)
        self.assertEqual(keys, ["a"])
        self.assertEqual(last, 10.0)

    def test_empty_read_within_idle_window_does_not_flush(self):
        d = KeyDecoder()
        d.feed(b"\x1b")                                        # a lone prefix stays in the buffer
        keys, last = ingest(d, b"", now=10.1, last_byte=10.0)   # only 100 ms passed
        self.assertEqual(keys, [])
        self.assertEqual(last, 10.0)                            # last_byte unchanged
        self.assertEqual(d.feed(b"[D"), [SINISTRA])             # the ESC is still there, key completes

    def test_empty_read_after_idle_window_flushes_once(self):
        d = KeyDecoder()
        d.feed(b"\x1b")
        keys, last = ingest(d, b"", now=10.4, last_byte=10.0)   # 400 ms, a real pause
        self.assertEqual(keys, [])
        self.assertEqual(d.take_unknown(), [b"\x1b"])
        # last_byte is now 10.4 so another empty read at 10.5 does not flush again
        keys, last2 = ingest(d, b"", now=10.5, last_byte=last)
        self.assertEqual(keys, [])
        self.assertEqual(last2, last)


class AppOverSerialTests(unittest.TestCase):
    def test_keys_drive_app_and_redraw(self):
        app = App(FakeShop())  # 80 columns
        port = FakePort()
        term = SerialTerminal(port, "ansi", margin=0)
        term.draw(app.screen(), full=True)
        port.out = b""
        for k in KeyDecoder().feed(b"5"):   # "Cerca" on the home screen
            app.key(k)
        term.draw(app.screen())
        self.assertIn(b"CERCA", port.out)


if __name__ == "__main__":
    unittest.main()
