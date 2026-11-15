import re, subprocess, sys
def check(changes, a, b):
    txt = open(changes).read()
    listed = set()
    for m in re.findall(r'`([^`\s]+)`', txt):
        m = m.strip()
        if '/' in m or m.endswith(('.py','.md','.css','.js','.kts','.yml','.swift','.xlsx','.csv','.json')):
            if re.match(r'^[\w./\-*]+$', m) and not m.startswith(('http','/api','/m','/')) and '.' in m.split('/')[-1]:
                listed.add(m)
    diff = subprocess.run(['git','diff','--name-only',a,b],capture_output=True,text=True).stdout.split()
    diff=set(diff)
    exp=set()
    for l in listed:
        if '*' in l:
            pat = re.compile('^'+re.escape(l).replace(r'\*','.*')+'$')
            exp |= {d for d in diff if pat.match(d)} or {l}
        else: exp.add(l)
    # listed but unchanged (only those that look like repo paths)
    unchanged = sorted(l for l in exp if l not in diff and not any(d.endswith('/'+l) or d==l for d in diff))
    unlisted = sorted(d for d in diff if d not in exp and not any(d.endswith('/'+l) for l in exp) and not d.startswith('docs/testing/screenshots'))
    print(f'== {changes}  {a}..{b}: listed {len(exp)}, changed {len(diff)}')
    print(' listed but not in diff:', unchanged)
    print(' changed but not listed:', unlisted)
check('docs/reports/2026-11-03-fullreview/CHANGES.md','29b488e','fac38d3')
check('docs/reports/2026-11-04-verification/CHANGES.md','369f6d6','7887351')
