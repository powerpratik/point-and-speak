import unittest

from PIL import Image, ImageDraw

from lookcore import Loop, Marker, Sample
from lookimg import KeyframeKeeper, annotate_overview, crop_around, crop_region, frame_changed, thumbnail

DISPLAY = (1000.0, 500.0)


def page(color=(240, 240, 240), size=(2000, 1000)):
    return Image.new("RGB", size, color)


class ChangeTests(unittest.TestCase):
    def test_identical_screens_are_unchanged(self):
        self.assertFalse(frame_changed(thumbnail(page()), thumbnail(page())))

    def test_a_small_cursor_sized_change_is_ignored(self):
        a, b = page(), page()
        ImageDraw.Draw(b).rectangle((900, 400, 930, 440), fill=(0, 0, 0))
        self.assertFalse(frame_changed(thumbnail(a), thumbnail(b)))

    def test_a_new_page_is_a_change(self):
        self.assertTrue(frame_changed(thumbnail(page()), thumbnail(page((20, 30, 60)))))

    def test_a_scroll_sized_change_is_a_change(self):
        a, b = page(), page()
        d = ImageDraw.Draw(b)
        for y in range(0, 1000, 80):
            d.rectangle((0, y, 2000, y + 40), fill=(10, 10, 10))
        self.assertTrue(frame_changed(thumbnail(a), thumbnail(b)))


class KeeperTests(unittest.TestCase):
    def test_a_static_screen_keeps_only_the_first_frame(self):
        k = KeyframeKeeper()
        kept = [k.offer(t, page()) for t in range(6)]
        self.assertEqual(kept, [True, False, False, False, False, False])
        self.assertEqual(len(k.frames), 1)

    def test_changes_add_frames_up_to_the_limit_then_replace_the_last(self):
        k = KeyframeKeeper(limit=2)
        for i, c in enumerate([(240, 240, 240), (10, 10, 10), (200, 20, 20), (20, 200, 20)]):
            k.offer(float(i), page(c))
        self.assertEqual(len(k.frames), 2)
        self.assertEqual(k.frames[-1][0], 3.0)


class AnnotateTests(unittest.TestCase):
    def test_marker_and_trail_are_drawn_and_the_image_is_downscaled(self):
        img = page(size=(2000, 1000))
        samples = [Sample(i / 10, 100 + i * 8, 250) for i in range(60)]
        marker = Marker(1, "this", 3.0, 500.0, 250.0, "point")
        out = annotate_overview(img, samples, [marker], [], DISPLAY, max_width=1000)
        self.assertEqual(out.size, (1000, 500))
        # the marker disc is blue where the marker is
        r, g, b = out.getpixel((508, 250))[:3]
        self.assertGreater(b, r + 40)
        # the trail is reddish somewhere along the path
        r2, g2, b2 = out.getpixel((300, 250))[:3]
        self.assertGreater(r2, b2)

    def test_loop_outline_is_drawn(self):
        pts = tuple((400 + 80 * __import__("math").cos(a / 10), 250 + 80 * __import__("math").sin(a / 10)) for a in range(64))
        loop = Loop(0, 1, (320, 170, 480, 330), (400, 250), pts)
        out = annotate_overview(page(), [Sample(0, 400, 250)], [], [loop], DISPLAY, max_width=1000)
        r, g, b = out.getpixel((480, 250))[:3]
        self.assertGreater(r, 200)
        self.assertLess(b, 120)

    def test_does_not_upscale_a_small_screenshot(self):
        out = annotate_overview(page(size=(800, 400)), [], [], [], (800.0, 400.0), max_width=1280)
        self.assertEqual(out.size, (800, 400))


class CropTests(unittest.TestCase):
    def test_crop_is_centred_and_native_resolution(self):
        img = page()
        ImageDraw.Draw(img).rectangle((990, 490, 1010, 510), fill=(255, 0, 0))  # screen point (500,250) at 2x
        c = crop_around(img, 500, 250, DISPLAY, (200, 100))
        self.assertEqual(c.size, (200, 100))
        self.assertEqual(c.getpixel((100, 50))[:3], (255, 0, 0))

    def test_crop_near_an_edge_stays_inside_the_screen(self):
        c = crop_around(page(), 2, 2, DISPLAY, (480, 320))
        self.assertEqual(c.size, (480, 320))

    def test_region_crop_pads_and_caps_size(self):
        c = crop_region(page(), (100, 100, 900, 450), DISPLAY, pad=20, max_side=900)
        self.assertLessEqual(max(c.size), 900)
        small = crop_region(page(), (100, 100, 200, 160), DISPLAY, pad=10)
        self.assertEqual(small.size, ((100 + 20) * 2, (60 + 20) * 2))


if __name__ == "__main__":
    unittest.main()
