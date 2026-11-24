"""回帖参数的单元测试：确认 pid（楼中楼）真的会被放进请求体。

不联网：把 tieba_post._request 换成假的，看它拼出来的表单。
"""
from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tieba_post  # noqa: E402


class ReplyFields(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def fake_request(url, data=None, referer=""):
            self.seen.append(urllib.parse.parse_qs((data or b"").decode("utf-8")))
            return {"no": 0}

        self._orig = tieba_post._request
        self._orig_tbs = tieba_post.get_tbs
        tieba_post._request = fake_request
        tieba_post.get_tbs = lambda: {"tbs": "x", "is_login": 1}

    def tearDown(self):
        tieba_post._request = self._orig
        tieba_post.get_tbs = self._orig_tbs

    def test_plain_reply_has_no_pid(self):
        tieba_post.reply(123, "普通回帖", "5soul")
        fields = self.seen[-1]
        self.assertNotIn("pid", fields)
        self.assertNotIn("is_feedback", fields)

    def test_floor_reply_carries_pid(self):
        tieba_post.reply(123, "回某一层", "5soul", pid=987654321)
        fields = self.seen[-1]
        self.assertEqual(fields["pid"], ["987654321"])
        self.assertEqual(fields["is_feedback"], ["1"])
        self.assertEqual(fields["tid"], ["123"])


if __name__ == "__main__":
    unittest.main()
