import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from repost_stats import PostStats, sort_posts

class StatsTests(unittest.TestCase):
    def test_time_sort_does_not_depend_on_metric_coverage(self):
        rows = [{'tid': i, 'at': f'11-{i:02d} 12:00'} for i in range(1, 5)]
        stats = {1: {'view': 20}, 3: {'view': 2}, 4: {'view': 0}}
        self.assertEqual([r['tid'] for r in sort_posts(rows, stats, 'at', 'asc')], [1, 2, 3, 4])
        self.assertEqual([r['tid'] for r in sort_posts(rows, stats, 'at', 'desc')], [4, 3, 2, 1])
        self.assertEqual([r['tid'] for r in sort_posts(rows, stats, 'view', 'asc')], [4, 3, 1, 2])
        self.assertEqual([r['tid'] for r in sort_posts(rows, stats, 'view', 'desc')], [1, 3, 4, 2])

    def test_old_values_remain_on_failure_and_zero_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'cache.json'
            file.write_text(json.dumps({'1': {'view': 30, 'reply': 2, 'agree': 0, 'title': '旧帖'}}), encoding='utf-8')
            store = PostStats(file, lambda tids: {}, clock=lambda: 1000)
            self.assertEqual(store.snapshot([1])[1]['updatedAt'], 0)
            store.apply([1], {})
            self.assertEqual(store.snapshot([1])[1]['view'], 30)
            self.assertEqual(store.snapshot([1])[1]['statsStatus'], 'error')
            store.apply([2], {2: {'view': 0, 'reply': 0, 'agree': 0, 'title': '新帖'}})
            restored = PostStats(file, lambda tids: {}, clock=lambda: 2000)
            self.assertEqual(restored.snapshot([2])[2]['view'], 0)
            self.assertEqual(restored.snapshot([2])[2]['updatedAt'], 1000)
            self.assertEqual(restored.snapshot([2])[2]['statsStatus'], 'stale')

    def test_refresh_requests_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PostStats(Path(directory) / 'cache.json', lambda tids: {}, clock=lambda: 1000)
            class Worker:
                def is_alive(self): return True
            store.worker = Worker()  # 只检查调度，不启动联网线程。
            store.request([1, 2, 3]); store.request([2, 1], foreground=True)
            self.assertEqual(list(store.pending), [2, 1, 3])
            store.inflight.add(4); store.request([4])
            self.assertNotIn(4, store.pending)

    def test_payload_global_totals_and_sort_before_paging(self):
        spec = importlib.util.spec_from_file_location('test_console', ROOT / 'console/server.py')
        server = importlib.util.module_from_spec(spec); spec.loader.exec_module(server)
        entries = [{'ok': True, 'item': f'a:{i}', 'url': f'https://tieba.baidu.com/p/{i}',
                    'at': f'11-{i:02d} 12:00', 'title': f'帖子{i}', 'forum': 'a'} for i in range(1, 26)]
        values = {i: {'view': i, 'reply': 26 - i, 'agree': i % 3,
                      'updatedAt': 1, 'statsStatus': 'stale', 'refreshing': False} for i in range(1, 26)}
        class FakeStats:
            def snapshot(self, tids): return {tid: values[tid] for tid in tids}
            def request(self, *args, **kwargs): pass
        with patch.object(server, '_STATS', FakeStats()), patch.object(server, '_repost_entries', return_value=entries):
            for key in ('at', 'view', 'reply', 'agree'):
                for order in ('asc', 'desc'):
                    first = server.repost_payload(1, 10, key, order)
                    second = server.repost_payload(2, 10, key, order)
                    self.assertEqual(first['totals']['view'], 325)
                    self.assertEqual(first['totals'], second['totals'])
                    self.assertFalse({r['tid'] for r in first['rows']} & {r['tid'] for r in second['rows']})
                    self.assertEqual(first['status']['nextAt'], second['status']['nextAt'])
                    self.assertEqual(first['totals']['covered'], 25)

if __name__ == '__main__':
    unittest.main()
