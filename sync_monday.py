#!/usr/bin/env python3
# 먼데이닷컴 보드 -> 자재코드별 항목 링크 매핑(monday_map.json) 생성 (GitHub Actions용)
# - 하위 아이템(및 상위 아이템)의 파일 컬럼(기본 '최종 도안')에 올라간 파일명 앞부분을 자재코드로 인식
# - 토큰은 환경변수 MONDAY_TOKEN (GitHub Secret)
import os, re, json, sys, datetime, urllib.request

TOKEN = os.environ.get('MONDAY_TOKEN', '').strip()
BOARD_ID = os.environ.get('MONDAY_BOARD_ID', '4057650308').strip()
ACCOUNT = os.environ.get('MONDAY_ACCOUNT', 'spigen').strip()
FILES_COL = os.environ.get('MONDAY_FILES_COLUMN', '최종 도안').strip()
OUT = 'monday_map.json'
API = 'https://api.monday.com/v2'

if not TOKEN:
    sys.exit('ERROR MONDAY_TOKEN 시크릿이 설정되지 않았습니다. (Settings → Secrets and variables → Actions)')

FRAG = '''
fragment F on Item {
  id name
  column_values { id type column { title }
    ... on FileValue { files { __typename
      ... on FileAssetValue { asset { id name } }
      ... on FileLinkValue { name url } } } }
  subitems { id name board { id }
    column_values { id type column { title }
      ... on FileValue { files { __typename
        ... on FileAssetValue { asset { id name } }
        ... on FileLinkValue { name url } } } } }
}'''
Q_FIRST = 'query($b:[ID!]){ boards(ids:$b){ items_page(limit:100){ cursor items{ ...F } } } }' + FRAG
Q_NEXT = 'query($c:String!){ next_items_page(cursor:$c, limit:100){ cursor items{ ...F } } }' + FRAG

def gql(query, variables):
    body = json.dumps({'query': query, 'variables': variables}).encode('utf-8')
    req = urllib.request.Request(API, data=body, headers={
        'Authorization': TOKEN, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as r:
        res = json.loads(r.read().decode('utf-8'))
    if res.get('errors'):
        raise SystemExit('ERROR monday API: ' + json.dumps(res['errors'], ensure_ascii=False)[:900])
    return res['data']

CODE_RE = re.compile(r'^\s*([A-Za-z0-9]+)')
def code_of(fname):
    m = CODE_RE.match(fname or '')
    if not m: return None
    c = m.group(1).upper()
    if len(c) < 4 or not any(ch.isdigit() for ch in c): return None
    return c

def file_names(col):
    out = []
    for f in (col.get('files') or []):
        if f.get('__typename') == 'FileAssetValue':
            a = f.get('asset') or {}
            if a.get('name'): out.append(a['name'])
        elif f.get('__typename') == 'FileLinkValue':
            if f.get('name'): out.append(f['name'])
    return out

def file_cols(cvs, strict=True):
    res = []
    for cv in cvs or []:
        if cv.get('type') != 'file': continue
        title = ((cv.get('column') or {}).get('title') or '').strip()
        if strict and title != FILES_COL: continue
        res.append(cv)
    return res

def fetch_all_items():
    items = []
    d = gql(Q_FIRST, {'b': [BOARD_ID]})
    boards = d.get('boards') or []
    if not boards: raise SystemExit(f'ERROR 보드 {BOARD_ID}를 찾지 못했습니다(토큰 권한/보드 번호 확인).')
    page = boards[0]['items_page']
    items += page['items']; cursor = page.get('cursor')
    while cursor:
        page = gql(Q_NEXT, {'c': cursor})['next_items_page']
        items += page['items']; cursor = page.get('cursor')
    return items

def build(items):
    mapping = {}
    stats = {'items': len(items), 'subitems': 0, 'files': 0, 'title_hits': 0}
    def add(code, url, parent_url, item_name, sub_name, fname):
        e = mapping.get(code)
        if not e:
            mapping[code] = {'url': url, 'parent_url': parent_url, 'item': item_name, 'sub': sub_name, 'files': [fname]}
        else:
            if fname not in e['files']: e['files'].append(fname)
            if url != e['url'] and url not in e.setdefault('alt', []): e['alt'].append(url)
    # 1차: 제목이 정확히 일치하는 파일 컬럼만
    for strict in (True, False):
        for it in items:
            parent_url = f'https://{ACCOUNT}.monday.com/boards/{BOARD_ID}/pulses/{it["id"]}'
            for cv in file_cols(it.get('column_values'), strict):
                stats['title_hits'] += 1
                for fn in file_names(cv):
                    stats['files'] += 1
                    c = code_of(fn)
                    if c: add(c, parent_url, parent_url, it.get('name'), None, fn)
            for sub in it.get('subitems') or []:
                if strict: stats['subitems'] += 1
                sb = (sub.get('board') or {}).get('id') or BOARD_ID
                sub_url = f'https://{ACCOUNT}.monday.com/boards/{sb}/pulses/{sub["id"]}'
                for cv in file_cols(sub.get('column_values'), strict):
                    stats['title_hits'] += 1
                    for fn in file_names(cv):
                        stats['files'] += 1
                        c = code_of(fn)
                        if c: add(c, sub_url, parent_url, it.get('name'), sub.get('name'), fn)
        if strict and stats['title_hits'] > 0: break
        if strict: print(f"WARN '{FILES_COL}' 제목의 파일 컬럼을 찾지 못해 모든 파일 컬럼을 검색합니다.")
    return mapping, stats

def main():
    items = fetch_all_items()
    mapping, st = build(items)
    out = {'_meta': {'generated_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                     'board': BOARD_ID, 'account': ACCOUNT, 'files_column': FILES_COL, **st, 'codes': len(mapping)},
           'map': dict(sorted(mapping.items()))}
    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    sample = ', '.join(list(mapping)[:5])
    print(f"OK 아이템 {st['items']} · 하위아이템 {st['subitems']} · 파일 {st['files']} → 자재코드 {len(mapping)}개 매핑 (예: {sample})")
    if not mapping:
        print("WARN 매핑된 코드가 0개입니다. 파일명이 자재코드로 시작하는지, 컬럼 제목이 맞는지 확인하세요.")

if __name__ == '__main__':
    main()
