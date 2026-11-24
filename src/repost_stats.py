"""只读帖子统计：单队列刷新，保留最后成功值，页面查询不等待网络。"""
from __future__ import annotations
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

FIELDS = ('view', 'reply', 'agree', 'title')

class PostStats:
    def __init__(self, file, fetch, clock=time.time):
        self.file, self.fetch, self.clock = Path(file), fetch, clock
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.rows, self.pending, self.inflight, self.retry = {}, OrderedDict(), set(), {}
        self.worker = None
        try:
            for tid, value in json.loads(self.file.read_text(encoding='utf-8')).items():
                self.rows[int(tid)] = {k: value.get(k, '' if k == 'title' else 0) for k in FIELDS}
                self.rows[int(tid)]['at'] = float(value.get('at') or 0)
        except (OSError, ValueError, TypeError):
            pass

    def snapshot(self, tids):
        with self.lock:
            now = self.clock()
            out = {}
            for tid in tids:
                value = self.rows.get(tid)
                queued = tid in self.pending or tid in self.inflight
                state = 'pending' if queued else 'error' if tid in self.retry else 'missing'
                if value is not None:
                    state = 'error' if tid in self.retry else 'fresh' if now - value['at'] <= 60 else 'stale'
                out[tid] = {**({k: value[k] for k in FIELDS} if value else {}),
                            'updatedAt': value['at'] if value else None,
                            'statsStatus': state, 'refreshing': queued}
            return out

    def request(self, tids, foreground=False):
        with self.lock:
            now = self.clock()
            for tid in reversed(list(dict.fromkeys(tids))) if foreground else dict.fromkeys(tids):
                value = self.rows.get(tid)
                if tid in self.inflight or now < self.retry.get(tid, 0):
                    continue
                if value and now - value['at'] < (30 if foreground else 300):
                    continue
                self.pending[tid] = None
                if foreground:
                    self.pending.move_to_end(tid, last=False)
            if self.pending:
                if not self.worker or not self.worker.is_alive():
                    self.worker = threading.Thread(target=self._run, daemon=True, name='post-stats')
                    self.worker.start()
                self.wake.set()

    def apply(self, tids, values):
        with self.lock:
            now = self.clock()
            for tid in tids:
                row = values.get(tid)
                if row is None:
                    self.retry[tid] = now + 60
                    continue
                self.rows[tid] = {**{k: row.get(k, '' if k == 'title' else 0) for k in FIELDS}, 'at': now}
                self.retry.pop(tid, None)
            if values:
                self.file.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.file.with_suffix('.tmp')
                temporary.write_text(json.dumps(self.rows, ensure_ascii=False), encoding='utf-8')
                os.replace(temporary, self.file)

    def _run(self):
        failures = 0
        while True:
            self.wake.wait(30)
            with self.lock:
                tids = [self.pending.popitem(last=False)[0] for _ in range(min(12, len(self.pending)))]
                self.inflight.update(tids)
                if not self.pending:
                    self.wake.clear()
            if not tids:
                continue
            try:
                try:
                    values = self.fetch(tids)
                except Exception:
                    values = {}
                self.apply(tids, values)
                failures = 0 if values else min(failures + 1, 4)
            finally:
                with self.lock:
                    self.inflight.difference_update(tids)
            time.sleep(min(120, 15 * 2 ** (failures - 1)) if failures else .5)

def post_time(value, now=None, tz=None):
    from datetime import datetime, timedelta
    now = now or datetime.now(tz).replace(tzinfo=None)
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        try:
            parsed = datetime.strptime(f'{now.year}-{value}', '%Y-%m-%d %H:%M')
            if parsed > now + timedelta(days=1):
                parsed = parsed.replace(year=now.year - 1)
        except (ValueError, TypeError):
            return 0
    return (parsed.replace(tzinfo=tz) if tz is not None and parsed.tzinfo is None else parsed).timestamp()

def sort_posts(rows, stats, key='at', order='desc', timezone=None):
    key = key if key in ('at', 'view', 'reply', 'agree') else 'at'
    tie = lambda r: (post_time(r.get('at'), tz=timezone), r['tid'])
    if key == 'at':
        return sorted(rows, key=tie, reverse=order != 'asc')
    known = [r for r in rows if key in stats.get(r['tid'], {})]
    unknown = [r for r in rows if key not in stats.get(r['tid'], {})]
    known.sort(key=lambda r: (int(stats[r['tid']][key]), *tie(r)), reverse=order != 'asc')
    return known + sorted(unknown, key=tie, reverse=True)
