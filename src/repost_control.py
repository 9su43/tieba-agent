"""Cross-process stop flag and last-moment write gate for the repost worker."""
import json
import time
from contextlib import contextmanager
from pathlib import Path
import settings

class RepostStopped(RuntimeError):pass

def stopped(config=None):
    if (settings.ROOT/'state'/'repost-stop.flag').exists():return True
    path=Path(config) if config else settings.ROOT/'config.json'
    try:return json.loads(path.read_text(encoding='utf-8')).get('repost',{}).get('enabled') is not True
    except (OSError,ValueError):return True

def set_enabled(enabled):
    state=settings.ROOT/'state';state.mkdir(exist_ok=True)
    flag=state/'repost-stop.flag'
    if enabled:flag.unlink(missing_ok=True)
    else:
        flag.write_text(str(time.time()),encoding='utf-8')
        (state/'repost-now.flag').unlink(missing_ok=True)

def status():
    try:
        row=json.loads((settings.ROOT/'state'/'repost-operation.json').read_text(encoding='utf-8'))
        return row if time.time()-float(row.get('since',0))<180 else {'phase':'idle'}
    except (OSError,ValueError,TypeError):return {'phase':'idle'}

@contextmanager
def submission(config=None,kind='thread'):
    if stopped(config):raise RepostStopped('搬运已停止，未提交发布请求')
    path=settings.ROOT/'state'/'repost-operation.json'
    settings.atomic_json(path,{'phase':'publishing','kind':kind,'since':time.time()})
    try:
        if stopped(config):raise RepostStopped('搬运已停止，未提交发布请求')
        yield
    finally:settings.atomic_json(path,{'phase':'idle','since':time.time()})
