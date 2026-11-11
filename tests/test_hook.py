import tempfile
import unittest
from home_media_follow.hook import main


class HookIsolationTests(unittest.TestCase):
    def test_unregistered_callback_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(main(['aabbccddeeff0011','1','D:/Other/video.mp4'],root=directory),1)
