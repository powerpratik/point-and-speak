import math
import unittest

from lookcore import (
    Sample,
    Word,
    analyze,
    annotate_transcript,
    detect_dwells,
    detect_loops,
    position_at,
    resolve_markers,
)

HZ = 60.0


def still(t0, t1, x, y):
    n = int((t1 - t0) * HZ)
    return [Sample(t0 + i / HZ, x, y) for i in range(n + 1)]


def line(t0, t1, x0, y0, x1, y1):
    n = max(2, int((t1 - t0) * HZ))
    return [Sample(t0 + (t1 - t0) * i / n, x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n + 1)]


def circle(t0, t1, cx, cy, r, turns=1.0, jitter=0.0):
    n = max(8, int((t1 - t0) * HZ))
    out = []
    for i in range(n + 1):
        a = 2 * math.pi * turns * i / n
        out.append(Sample(t0 + (t1 - t0) * i / n, cx + r * math.cos(a) + jitter * ((i * 7) % 3 - 1), cy + r * math.sin(a)))
    return out


class PositionTests(unittest.TestCase):
    def test_interpolates_between_samples(self):
        s = [Sample(0, 0, 0), Sample(1, 100, 50)]
        self.assertEqual(position_at(s, 0.5), (50.0, 25.0))

    def test_clamps_outside_the_trace(self):
        s = [Sample(1, 10, 20), Sample(2, 30, 40)]
        self.assertEqual(position_at(s, 0), (10, 20))
        self.assertEqual(position_at(s, 9), (30, 40))

    def test_empty_trace(self):
        self.assertEqual(position_at([], 1.0), (0.0, 0.0))


class DwellTests(unittest.TestCase):
    def test_a_pause_is_one_dwell(self):
        d = detect_dwells(still(0, 1.0, 500, 300))
        self.assertEqual(len(d), 1)
        self.assertAlmostEqual(d[0].x, 500)
        self.assertAlmostEqual(d[0].t1 - d[0].t0, 1.0, places=1)

    def test_moving_pointer_has_no_dwell(self):
        self.assertEqual(detect_dwells(line(0, 2, 0, 0, 1200, 600)), [])

    def test_a_short_pause_is_ignored(self):
        self.assertEqual(detect_dwells(still(0, 0.2, 10, 10)), [])

    def test_tiny_jitter_still_counts_as_a_dwell(self):
        s = [Sample(i / HZ, 400 + (i % 3), 200 + (i % 2)) for i in range(90)]
        self.assertEqual(len(detect_dwells(s)), 1)

    def test_two_pauses_with_a_move_between(self):
        s = still(0, 0.6, 100, 100) + line(0.6, 1.0, 100, 100, 700, 400) + still(1.0, 1.6, 700, 400)
        d = detect_dwells(s)
        self.assertEqual(len(d), 2)
        self.assertLess(d[0].x, 200)
        self.assertGreater(d[1].x, 600)


class LoopTests(unittest.TestCase):
    def test_a_circle_is_a_loop_with_its_bounding_box(self):
        loops = detect_loops(circle(0, 1.5, 800, 500, 80))
        self.assertEqual(len(loops), 1)
        left, top, right, bottom = loops[0].bbox
        self.assertAlmostEqual(left, 720, delta=6)
        self.assertAlmostEqual(right, 880, delta=6)
        self.assertAlmostEqual(top, 420, delta=6)
        self.assertAlmostEqual(bottom, 580, delta=6)

    def test_a_sloppy_loop_that_does_not_quite_close_still_counts(self):
        s = circle(0, 1.5, 300, 300, 90, turns=0.92, jitter=2.0)
        self.assertEqual(len(detect_loops(s)), 1)

    def test_a_straight_line_is_not_a_loop(self):
        self.assertEqual(detect_loops(line(0, 2, 0, 0, 1500, 800)), [])

    def test_back_and_forth_is_not_a_loop(self):
        s = line(0, 0.5, 100, 100, 500, 110) + line(0.5, 1.0, 500, 110, 100, 120) + line(1.0, 1.5, 100, 120, 500, 100)
        self.assertEqual(detect_loops(s), [])

    def test_a_tiny_wiggle_is_not_a_loop(self):
        self.assertEqual(detect_loops(circle(0, 1.0, 400, 400, 6)), [])

    def test_loop_found_in_the_middle_of_other_movement(self):
        s = line(0, 0.5, 0, 0, 600, 400) + circle(0.5, 2.0, 600, 400, 70) + line(2.0, 2.5, 670, 400, 1200, 200)
        self.assertEqual(len(detect_loops(s)), 1)


class MarkerTests(unittest.TestCase):
    def setUp(self):
        # pointer at A until 1.0s, moves, then at B from 2.0s
        self.samples = still(0, 1.0, 200, 150) + line(1.0, 2.0, 200, 150, 900, 600) + still(2.0, 3.0, 900, 600)
        self.words = [
            Word("change", 0.0, 0.3),
            Word("this", 0.4, 0.6),
            Word("to", 0.7, 0.8),
            Word("blue", 0.9, 1.1),
            Word("and", 1.5, 1.6),
            Word("that", 2.2, 2.4),
            Word("to", 2.5, 2.6),
            Word("red", 2.7, 2.9),
        ]

    def test_this_and_that_point_where_the_pointer_was(self):
        d = detect_dwells(self.samples)
        m = resolve_markers(self.words, self.samples, d, [])
        self.assertEqual([x.n for x in m], [1, 2])
        self.assertAlmostEqual(m[0].x, 200, delta=3)
        self.assertAlmostEqual(m[1].x, 900, delta=3)
        self.assertEqual(m[0].kind, "dwell")

    def test_words_that_do_not_point_get_no_marker(self):
        m = resolve_markers([Word("rename", 0, 0.3), Word("the", 0.3, 0.4), Word("file", 0.4, 0.7)], self.samples, [], [])
        self.assertEqual(m, [])

    def test_repeated_this_in_one_place_merges_into_one_marker(self):
        words = [Word("this", 0.1, 0.2), Word("and", 0.3, 0.4), Word("this", 0.5, 0.6)]
        m = resolve_markers(words, self.samples, [], [])
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].word_indexes, [0, 2])

    def test_punctuation_does_not_hide_a_pointing_word(self):
        m = resolve_markers([Word("This,", 0.1, 0.3)], self.samples, [], [])
        self.assertEqual(len(m), 1)

    def test_circled_region_points_at_the_loop(self):
        s = still(0, 0.3, 50, 50) + circle(0.3, 1.8, 600, 400, 80) + still(1.8, 3.0, 650, 410)
        words = [Word("fix", 2.0, 2.2), Word("my", 2.2, 2.3), Word("circled", 2.3, 2.6), Word("region", 2.6, 3.0)]
        loops = detect_loops(s)
        m = resolve_markers(words, s, detect_dwells(s), loops)
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].kind, "loop")
        self.assertIsNotNone(m[0].bbox)

    def test_circled_with_no_loop_drawn_gets_no_marker(self):
        m = resolve_markers([Word("circled", 0.5, 0.9)], self.samples, [], [])
        self.assertEqual(m, [])

    def test_a_loop_drawn_long_ago_is_not_matched(self):
        s = circle(0, 1.5, 600, 400, 80) + still(1.5, 12.0, 100, 100)
        loops = detect_loops(s)
        m = resolve_markers([Word("circled", 11.0, 11.4)], s, [], loops)
        self.assertEqual(m, [])


class SnapTests(unittest.TestCase):
    def test_a_word_said_while_arriving_snaps_to_the_rest_point(self):
        # moving until 3.0s, resting at (900,600) from 3.0s to 4.5s; the word is said at 2.7s
        s = line(0, 3.0, 100, 100, 900, 600) + still(3.0, 4.5, 900, 600)
        words = [Word("what", 0.1, 0.4), Word("is", 0.5, 0.7), Word("here", 2.5, 2.9)]
        d = detect_dwells(s)
        m = resolve_markers(words, s, d, [])
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].kind, "dwell")
        self.assertAlmostEqual(m[0].x, 900, delta=3)
        self.assertAlmostEqual(m[0].y, 600, delta=3)

    def test_a_word_said_long_before_any_rest_stays_at_the_pointer(self):
        s = line(0, 4.0, 100, 100, 900, 600) + still(4.0, 5.5, 900, 600)
        m = resolve_markers([Word("this", 0.4, 0.6)], s, detect_dwells(s), [])
        self.assertEqual(m[0].kind, "point")
        self.assertLess(m[0].x, 300)

    def test_a_rest_point_covering_the_word_wins_over_a_later_one(self):
        s = still(0, 1.5, 100, 100) + line(1.5, 2.0, 100, 100, 800, 500) + still(2.0, 3.5, 800, 500)
        m = resolve_markers([Word("this", 0.6, 0.9)], s, detect_dwells(s), [])
        self.assertAlmostEqual(m[0].x, 100, delta=3)


class SameSpotTests(unittest.TestCase):
    def test_pointing_words_far_apart_in_time_over_one_rest_become_one_marker(self):
        s = still(0, 9.0, 600, 400)
        words = [Word("that", 1.0, 1.2), Word("this", 4.0, 4.2), Word("those", 8.0, 8.3)]
        m = resolve_markers(words, s, detect_dwells(s), [])
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].word_indexes, [0, 1, 2])

    def test_two_different_rests_stay_two_markers(self):
        s = still(0, 3, 100, 100) + line(3, 4, 100, 100, 900, 600) + still(4, 7, 900, 600)
        words = [Word("this", 1.0, 1.2), Word("that", 5.0, 5.2)]
        self.assertEqual(len(resolve_markers(words, s, detect_dwells(s), [])), 2)


class TranscriptTests(unittest.TestCase):
    def test_marks_each_pointing_word(self):
        samples = still(0, 3, 10, 10)
        words = [Word("change", 0, 0.2), Word("this", 0.3, 0.5), Word("now", 0.6, 0.8)]
        d, l, m = analyze(words, samples)
        self.assertEqual(annotate_transcript(words, m), "change this[@1] now")

    def test_no_markers_leaves_the_text_alone(self):
        words = [Word("hello", 0, 0.2), Word("there", 0.3, 0.5)]
        self.assertEqual(annotate_transcript(words, []), "hello there")


if __name__ == "__main__":
    unittest.main()
