"""Scan the exact staged/tracked publication files. Never print a matched secret."""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SENSITIVE=re.compile(r"(?:^|/)(?:state|logs|\.venv|node_modules|\.dsh)(?:/|$)|(?:^|/)(?:config\.json|auth\.json|api-profiles\.json|\.env)$|\.(?:db|sqlite|log|bak)(?:$|[.-])",re.I)
PATTERNS={
    "private-windows-home":re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\s'\"/\\]+",re.I),
    "private-absolute-project":re.compile(r"[A-Za-z]:[\\/](?:[^\s'\"/\\]+[\\/])*(?:Plugins|Documents)[\\/]",re.I),
    "provider-key":re.compile(r"(?:sk-[A-Za-z0-9_-]{24,}|AIza[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9_]{25,}|github_pat_[A-Za-z0-9_]{30,})"),
    "private-key":re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "cookie-value":re.compile(r"\bBDUSS=[A-Za-z0-9_~=-]{30,}"),
}
def audit(files, staged=False):
    failures=[]
    for name in files:
        name=name.replace('\\','/')
        if SENSITIVE.search(name):failures.append({"file":name,"rule":"runtime-or-secret-file"});continue
        if name.startswith(('data/','knowledge/')) and not name.endswith('/README.md'):failures.append({"file":name,"rule":"private-corpus"});continue
        raw=subprocess.check_output(['git','show',':'+name],cwd=ROOT) if staged else (ROOT/name).read_bytes()
        if len(raw)>2*1024*1024:failures.append({"file":name,"rule":"unexpected-large-file"});continue
        text=raw.decode('utf-8','replace')
        for rule,pattern in PATTERNS.items():
            if pattern.search(text):failures.append({"file":name,"rule":rule})
    return failures
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--staged',action='store_true');args=ap.parse_args()
    command=['git','diff','--cached','--name-only','--diff-filter=ACMR','-z'] if args.staged else ['git','ls-files','-z']
    result=subprocess.run(command,cwd=ROOT,capture_output=True)
    if result.returncode:raise SystemExit('请先初始化 Git 仓库；审计只针对实际待发布文件')
    files=[s for s in result.stdout.decode().split('\0') if s]
    if not files:raise SystemExit('没有可审计的文件')
    failures=audit(files,args.staged)
    print(json.dumps({'files':len(files),'passed':not failures,'failures':failures},ensure_ascii=False))
    raise SystemExit(bool(failures))
if __name__=='__main__':main()
