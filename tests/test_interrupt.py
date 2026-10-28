"""A drawing can be stopped by a command: sent in pieces, with the port watched in between."""
import unittest

from totem.app import INDICE, App
from totem.terminal import KeyDecoder, KeyWatcher, SerialTerminal, chunks, encode_screen


class LinePort:
    """A port that records every write, can have bytes 'typed' on it after N pieces,
    and counts the flushes (the wait for a piece to leave the wire)."""
    def __init__(self, keys_after=None):
        self.writes, self.flushes = [], 0
        self.inbox = b""
        self.keys_after = dict(keys_after or {})      # piece number -> bytes that arrive then

    @property
    def in_waiting(self):
        return len(self.inbox)

    def read(self, n=1):
        out, self.inbox = self.inbox[:n], self.inbox[n:]
        return out

    def write(self, data):
        self.writes.append(data)
        self.inbox += self.keys_after.pop(len(self.writes), b"")

    def flush(self):
        self.flushes += 1


class FakeShop:
    def collections(self): return []
    def refresh(self): pass


def screens():
    app = App(FakeShop())
    home = app.screen()
    app.key("0")
    return home, app.screen()          # the home and the Credits screen


class ChunkTests(unittest.TestCase):
    def test_pieces_put_back_together_are_the_original(self):
        home, _ = screens()
        data = encode_screen(home, None, "ansi")
        for size in (8, 24, 100, 5000):
            self.assertEqual(b"".join(chunks(data, size)), data)

    def test_a_piece_never_ends_inside_an_escape_sequence(self):
        home, credits = screens()
        data = encode_screen(credits, None, "ansi") + encode_screen(home, credits, "ansi")
        for size in (8, 13, 24):
            for piece in chunks(data, size):
                tail = piece[-12:]
                esc = tail.rfind(b"\x1b")
                if esc != -1 and tail[esc + 1:esc + 2] == b"[":
                    body = tail[esc + 2:]
                    self.assertTrue(any(0x40 <= b <= 0x7E for b in body),
                                    f"piece ends inside {tail!r}")

    def test_pieces_are_about_the_requested_size(self):
        data = b"x" * 1000
        sizes = [len(p) for p in chunks(data, 24)]
        self.assertTrue(all(s == 24 for s in sizes[:-1]))
        self.assertEqual(sum(sizes), 1000)

    def test_a_vt52_cursor_jump_is_never_cut_in_half(self):
        # ESC Y row col is four bytes; a piece must not end between them, or the
        # terminal would eat the next write as the missing row/col coordinates.
        jump = b"\x1bY" + bytes([32 + 5, 32 + 10])
        data = b"x" * 20 + jump + b"y" * 20
        for size in range(5, 25):
            for piece in chunks(data, size):
                self.assertFalse(piece.endswith(b"\x1b"), f"size={size}: {piece!r}")
                self.assertFalse(piece.endswith(b"\x1bY"), f"size={size}: {piece!r}")
                self.assertFalse(piece.endswith(b"\x1bY" + bytes([32 + 5])),
                                 f"size={size}: {piece!r}")
            self.assertEqual(b"".join(chunks(data, size)), data)


class DrawingTests(unittest.TestCase):
    def test_without_abort_the_screen_goes_out_in_one_write_as_before(self):
        home, _ = screens()
        port = LinePort()
        term = SerialTerminal(port, "ansi")
        term.draw(home, full=True)
        self.assertEqual(len(port.writes), 1)

    def test_with_abort_it_goes_out_in_pieces_each_one_waited_for(self):
        home, _ = screens()
        port = LinePort()
        term = SerialTerminal(port, "ansi")
        term.draw(home, full=True, abort=lambda: False)
        self.assertGreater(len(port.writes), 20)
        self.assertEqual(port.flushes, len(port.writes))
        self.assertEqual(b"".join(port.writes), encode_screen(home, None, "ansi", 0, True))
        self.assertEqual(term.prev, home)

    def test_a_command_in_the_middle_stops_the_drawing_and_the_next_one_is_complete(self):
        home, credits = screens()
        port = LinePort(keys_after={5: b"\x1b[C"})                 # the right arrow, after 5 pieces
        watcher = KeyWatcher(port, KeyDecoder())
        term = SerialTerminal(port, "ansi")
        secs = term.draw(home, full=True, abort=watcher.interrupted)
        full = encode_screen(home, None, "ansi", 0, True)
        self.assertEqual(len(port.writes), 5)                      # nothing more was sent after the key
        self.assertLess(len(b"".join(port.writes)), len(full))
        self.assertIsNone(term.prev)                               # the terminal holds half a screen
        self.assertLess(secs, len(full) * 10 / 1200)
        self.assertEqual(watcher.take(), ["DESTRA"])
        # the screen for that command is then drawn in full, from a clear
        port.writes.clear()
        term.draw(credits, abort=watcher.interrupted)
        self.assertTrue(port.writes[0].startswith(b"\x1b[2J"))
        self.assertEqual(b"".join(port.writes), encode_screen(credits, None, "ansi"))

    def test_try_draw_passes_the_abort_along(self):
        home, _ = screens()
        port = LinePort(keys_after={3: b"x"})
        watcher = KeyWatcher(port, KeyDecoder())
        SerialTerminal(port, "ansi").try_draw(home, full=True, abort=watcher.interrupted)
        self.assertEqual(len(port.writes), 3)


class WatcherTests(unittest.TestCase):
    def test_known_keys_interrupt_and_wait_for_the_main_loop(self):
        port = LinePort()
        w = KeyWatcher(port, KeyDecoder({INDICE: [b"\x1bOP"]}))
        self.assertFalse(w.interrupted())                          # nothing arrived
        port.inbox = b"\x1bOP"
        self.assertTrue(w.interrupted())
        self.assertTrue(w.interrupted())                           # still pending until taken
        self.assertEqual(w.take(), [INDICE])
        self.assertFalse(w.interrupted())

    def test_noise_and_unknown_keys_do_not_interrupt(self):
        seen = []
        port = LinePort()
        w = KeyWatcher(port, KeyDecoder(), log=seen.append)
        port.inbox = b"\x1b[A\x00\x01"                              # up arrow (unknown) and control noise
        self.assertFalse(w.interrupted())
        self.assertEqual(seen, [b"\x1b[A"])                        # logged, as a key to learn
        self.assertEqual(w.take(), [])

    def test_several_commands_typed_ahead_are_all_kept_in_order(self):
        port = LinePort()
        w = KeyWatcher(port, KeyDecoder())
        port.inbox = b"2"
        self.assertTrue(w.interrupted())
        port.inbox = b"a\x1b[D"
        w.interrupted()
        self.assertEqual(w.take(), ["2", "a", "SINISTRA"])

    def test_a_key_sequence_split_between_two_checks_is_put_together(self):
        port = LinePort()
        w = KeyWatcher(port, KeyDecoder())
        port.inbox = b"\x1b"
        self.assertFalse(w.interrupted())                          # half an arrow: wait
        port.inbox = b"[C"
        self.assertTrue(w.interrupted())
        self.assertEqual(w.take(), ["DESTRA"])

    def test_a_port_without_in_waiting_never_interrupts(self):
        class Plain:
            def read(self, n=1): return b""
        self.assertFalse(KeyWatcher(Plain(), KeyDecoder()).interrupted())


if __name__ == "__main__":
    unittest.main()
