#!/usr/bin/env python3
# 먼데이닷컴 보드(여러 개) -> 자재코드별 항목 링크 매핑(monday_map.json) 생성 (GitHub Actions용)
# - 상위/하위 아이템의 파일 컬럼(기본 '최종 도안')에 올라간 파일명 앞부분을 자재코드로 인식
# - 같은 코드가 여러 곳에 있으면 '가장 최근 업로드' 파일의 항목을 링크, 나머지는 alt로 보관
# - 토큰은 환경변수 MONDAY_TOKEN (GitHub Secret)
import os, re, json, sys, time, datetime, urllib.request, urllib.error

TOKEN = os.environ.get('MONDAY_TOKEN', '').strip()
BOARD_IDS = [b.strip() for b in os.environ.get('MONDAY_BOARD_IDS', os.environ.get('MONDAY_BOARD_ID', '4057650308')).split(',') if b.strip()]
ACCOUNT = os.environ.get('MONDAY_ACCOUNT', 'spigen').strip()
FILES_COLS = [c.strip().lower().replace(' ', '') for c in os.environ.get('MONDAY_FILES_COLUMN', '최종 도안').split(',') if c.strip()]
CODE_PATTERN = os.environ.get('MONDAY_CODE_PATTERN', r'^[\s\[\(\{]*([A-Za-z0-9]{4,})')
ALL_TOKENS = os.environ.get('MONDAY_ALL_TOKENS', 'true').lower() == 'true'   # 파일명 안의 다른 코드(예: SKU)도 함께 인식
PAGE = int(os.environ.get('MONDAY_PAGE_SIZE', '25'))   # 한 번에 가져올 아이템 수(작을수록 안정적)
OUT = 'monday_map.json'
API = 'https://api.monday.com/v2'

if not TOKEN:
    sys.exit('ERROR MONDAY_TOKEN 시크릿이 설정되지 않았습니다. (Settings → Secrets and variables → Actions)')

class ApiError(Exception): pass

# 1단계: 아이템 목록만 가볍게 (id, 이름, 하위아이템 id)
Q_LIST = 'query($b:[ID!],$l:Int!){ boards(ids:$b){ id name items_page(limit:$l){ cursor items{ id name subitems{ id } } } } }'
Q_LIST_NEXT = 'query($c:String!,$l:Int!){ next_items_page(cursor:$c, limit:$l){ cursor items{ id name subitems{ id } } } }'
# 2단계: 상세(파일 컬럼 + 카드 첨부파일)를 id 묶음으로
FILES = ('files { __typename '
         '... on FileAssetValue { created_at name asset { id name created_at } } '
         '... on FileLinkValue { created_at name url } }')
FRAG_FULL = ('fragment D on Item { id name board { id } assets(assets_source: gallery) { id name created_at } '
             'column_values(types: [file]) { id type column { title } ... on FileValue { ' + FILES + ' } } }')
FRAG_COLS = ('fragment D on Item { id name board { id } '
             'column_values(types: [file]) { id type column { title } ... on FileValue { ' + FILES + ' } } }')
Q_DETAIL = 'query($ids:[ID!]){ items(ids:$ids){ ...D } }'
BATCH = int(os.environ.get('MONDAY_BATCH', '20'))
USE_GALLERY = True
gallery_fail = 0

def gql(query, variables, tries=4):
    body = json.dumps({'query': query, 'variables': variables}).encode('utf-8')
    for i in range(tries):
        try:
            req = urllib.request.Request(API, data=body, headers={'Authorization': TOKEN, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                wait = 10 * (i + 1); print(f'WARN HTTP {e.code} — {wait}s 후 재시도'); time.sleep(wait); continue
            raise ApiError(f'HTTP {e.code}: {e.read().decode("utf-8","replace")[:300]}')
        except (urllib.error.URLError, TimeoutError) as e:
            if i < tries - 1: time.sleep(10 * (i + 1)); continue
            raise ApiError(f'네트워크 오류: {e}')
        errs = res.get('errors')
        if errs:
            msg = json.dumps(errs, ensure_ascii=False)
            if ('omplexity' in msg or 'rate' in msg.lower()) and i < tries - 1:
                print('WARN API 한도 초과 — 60s 후 재시도'); time.sleep(60); continue
            if ('INTERNAL_SERVER_ERROR' in msg or 'DOWNSTREAM' in msg or 'status_code": 5' in msg) and i < tries - 1:
                time.sleep(5 * (i + 1)); continue
            raise ApiError(msg[:500])
        return res['data']
    raise ApiError('재시도 한도 초과')

def list_items(bid):
    d = gql(Q_LIST, {'b': [bid], 'l': PAGE})
    boards = d.get('boards') or []
    if not boards: raise SystemExit(f'ERROR 보드 {bid}를 찾지 못했습니다(토큰 권한/보드 번호 확인).')
    bname = boards[0].get('name') or bid
    page = boards[0]['items_page']; items = list(page['items']); cursor = page.get('cursor')
    while cursor:
        page = gql(Q_LIST_NEXT, {'c': cursor, 'l': PAGE})['next_items_page']; items += page['items']; cursor = page.get('cursor')
    return bname, items

def fetch_details(ids):
    """id 목록 -> {id: 상세}. 실패 시 반으로 쪼개 문제 항목만 건너뜀. 첨부파일 조회가 계속 막히면 컬럼 파일만."""
    global USE_GALLERY, gallery_fail
    out = {}
    def go(chunk):
        global USE_GALLERY, gallery_fail
        frag = FRAG_FULL if USE_GALLERY else FRAG_COLS
        try:
            for it in gql(Q_DETAIL + frag, {'ids': chunk})['items']: out[it['id']] = it
            return
        except ApiError as e:
            if len(chunk) > 1:
                mid = len(chunk) // 2; go(chunk[:mid]); go(chunk[mid:]); return
            if USE_GALLERY:   # 첨부파일 없이 한 번 더
                try:
                    for it in gql(Q_DETAIL + FRAG_COLS, {'ids': chunk}, tries=2)['items']: out[it['id']] = it
                    gallery_fail += 1
                    print(f'WARN 항목 {chunk[0]}: 첨부파일 조회 실패 → 컬럼 파일만 사용')
                    if gallery_fail >= 5:
                        USE_GALLERY = False; print('WARN 첨부파일 조회가 반복 실패해 이후 항목은 컬럼 파일만 읽습니다.')
                    return
                except ApiError as e2:
                    e = e2
            print(f'WARN 항목 {chunk[0]} 읽기 실패 — 건너뜀 ({str(e)[:120]})')
    for i in range(0, len(ids), BATCH):
        go(ids[i:i + BATCH])
        if (i // BATCH) % 10 == 9: print(f'  … {min(i + BATCH, len(ids))}/{len(ids)}')
    return out

def fetch_board(bid):
    bname, lst = list_items(bid)
    item_ids = [it['id'] for it in lst]
    sub_ids = [s['id'] for it in lst for s in (it.get('subitems') or [])]
    print(f"보드 '{bname}'({bid}): 아이템 {len(item_ids)} · 하위아이템 {len(sub_ids)} 상세 조회 중…")
    det = fetch_details(item_ids); sdet = fetch_details(sub_ids)
    items = []
    for it in lst:
        d = det.get(it['id'], {'id': it['id'], 'name': it.get('name')})
        d['subitems'] = [sdet[s['id']] for s in (it.get('subitems') or []) if s['id'] in sdet]
        items.append(d)
    return bname, items

CODE_RE = re.compile(CODE_PATTERN)
TOKEN_RE = re.compile(r'[A-Za-z0-9]+')
def codes_of(fname):
    """파일명 -> 코드 목록. 맨 앞 토큰(자재코드) + (옵션) 영문+숫자 섞인 5자 이상 토큰(SKU 등)"""
    base = re.sub(r'\.[A-Za-z0-9]{2,5}$', '', fname or '')   # 확장자 제거
    out = []
    m = CODE_RE.match(base)
    if m:
        c = m.group(1).upper()
        if any(ch.isdigit() for ch in c): out.append(c)
    if ALL_TOKENS:
        for t in TOKEN_RE.findall(base):
            u = t.upper()
            if len(u) >= 5 and any(ch.isdigit() for ch in u) and any(ch.isalpha() for ch in u) and u not in out:
                out.append(u)
    return out

def view_rank(fname):
    """브라우저에서 바로 보이는 형식 우선: 이미지 0, PDF 1, 기타(ai/psd 등) 2"""
    ext = (fname or '').rsplit('.', 1)[-1].lower() if '.' in (fname or '') else ''
    return 0 if ext in ('jpg','jpeg','png','gif','webp') else 1 if ext == 'pdf' else 2

def norm_title(t): return (t or '').strip().lower().replace(' ', '')

def files_of(cv):
    """파일 컬럼 값 -> [(파일명, 업로드시각ISO, asset_id 또는 None, 외부링크 또는 None)]"""
    out = []
    for f in (cv.get('files') or []):
        tn = f.get('__typename')
        if tn == 'FileAssetValue':
            a = f.get('asset') or {}
            name = a.get('name') or f.get('name'); ts = f.get('created_at') or a.get('created_at') or ''
            if name: out.append((name, ts, a.get('id'), None))
        elif tn == 'FileLinkValue':
            name = f.get('name'); ts = f.get('created_at') or ''
            if name: out.append((name, ts, None, f.get('url')))
    return out

def file_cols(cvs, strict):
    return [cv for cv in (cvs or []) if cv.get('type') == 'file' and (not strict or norm_title((cv.get('column') or {}).get('title')) in FILES_COLS)]

def build(board_items):
    """board_items: [(board_id, board_name, items)] -> mapping (최신 업로드 우선)"""
    cands = {}   # code -> [entry...]
    stats = {'boards': len(board_items), 'items': 0, 'subitems': 0, 'files': 0, 'title_hits': 0}
    def push(code, **e):
        cands.setdefault(code, []).append(e)
    seen_assets = set()
    def gallery(node, url_base, parent_url, bname, item_name, sub_name):
        for a in (node.get('assets') or []):
            if not a.get('id') or a['id'] in seen_assets: continue
            seen_assets.add(a['id']); stats['files'] += 1; stats['gallery'] = stats.get('gallery', 0) + 1
            cs = codes_of(a.get('name'))
            for c in cs:
                push(c, url=f"{url_base}?asset_id={a['id']}", parent_url=parent_url, board=bname, item=item_name, sub=sub_name, file=a.get('name'), updated=a.get('created_at') or '', lead=cs[0])
    for bid, bname, items in board_items:
        stats['items'] += len(items)
        hits = 0
        for it in items:   # 카드 첨부파일(파일 탭)
            purl = f'https://{ACCOUNT}.monday.com/boards/{bid}/pulses/{it["id"]}'
            gallery(it, purl, purl, bname, it.get('name'), None)
            for sub in it.get('subitems') or []:
                sb = (sub.get('board') or {}).get('id') or bid
                gallery(sub, f'https://{ACCOUNT}.monday.com/boards/{sb}/pulses/{sub["id"]}', purl, bname, it.get('name'), sub.get('name'))
        for strict in (True, False):
            for it in items:
                parent_url = f'https://{ACCOUNT}.monday.com/boards/{bid}/pulses/{it["id"]}'
                for cv in file_cols(it.get('column_values'), strict):
                    hits += 1
                    for fn, ts, aid, link in files_of(cv):
                        if aid and aid in seen_assets: continue
                        if aid: seen_assets.add(aid)
                        stats['files'] += 1
                        url = link or (f'{parent_url}?asset_id={aid}' if aid else parent_url)
                        cs = codes_of(fn)
                        for c in cs:
                            push(c, url=url, parent_url=parent_url, board=bname, item=it.get('name'), sub=None, file=fn, updated=ts, lead=cs[0])
                for sub in it.get('subitems') or []:
                    if strict: stats['subitems'] += 1
                    sb = (sub.get('board') or {}).get('id') or bid
                    sub_url = f'https://{ACCOUNT}.monday.com/boards/{sb}/pulses/{sub["id"]}'
                    for cv in file_cols(sub.get('column_values'), strict):
                        hits += 1
                        for fn, ts, aid, link in files_of(cv):
                            if aid and aid in seen_assets: continue
                            if aid: seen_assets.add(aid)
                            stats['files'] += 1
                            url = link or (f'{sub_url}?asset_id={aid}' if aid else sub_url)
                            cs = codes_of(fn)
                            for c in cs:
                                push(c, url=url, parent_url=parent_url, board=bname, item=it.get('name'), sub=sub.get('name'), file=fn, updated=ts, lead=cs[0])
            if hits > 0: break
            if strict: print(f"INFO 보드 '{bname}': '{', '.join(FILES_COLS)}' 제목의 파일 컬럼이 없어 모든 파일 컬럼(+카드 첨부파일)을 검색합니다.")
        stats['title_hits'] += hits
    mapping = {}
    for code, es in cands.items():
        es.sort(key=lambda e: ((e.get('updated') or '')[:10], -view_rank(e.get('file'))), reverse=True)   # 최신 날짜 먼저, 같은 날이면 이미지>PDF>기타
        top = es[0]
        entry = {'url': top['url'], 'parent_url': top['parent_url'], 'board': top['board'], 'item': top['item'],
                 'sub': top['sub'], 'file': top['file'], 'updated': (top.get('updated') or '')[:10], 'files': []}
        seen = set()
        for e in es:   # 최신 업로드 순, 파일(링크)별 상세
            if e['url'] in seen: continue
            seen.add(e['url'])
            entry['files'].append({'name': e['file'], 'url': e['url'], 'updated': (e.get('updated') or '')[:10],
                                   'code': e.get('lead') or code, 'sub': e['sub'], 'board': e['board']})
        alts = [f['url'] for f in entry['files'][1:]]
        if alts: entry['alt'] = alts
        mapping[code] = entry
    return mapping, stats

def main():
    board_items = []
    for bid in BOARD_IDS:
        bname, items = fetch_board(bid)
        print(f"보드 '{bname}'({bid}): 아이템 {len(items)}개")
        board_items.append((bid, bname, items))
    mapping, st = build(board_items)
    # 보호: 결과 0건인데 기존 파일에 데이터가 있으면 덮어쓰지 않음(일시 장애로 버튼이 전부 사라지는 것 방지)
    if not mapping and os.path.exists(OUT):
        try: old = len((json.load(open(OUT, encoding='utf-8')) or {}).get('map') or {})
        except Exception: old = 0
        if old > 0:
            sys.exit(f'ERROR 이번 결과가 0건이라 기존 {old}건 목록을 보존합니다. 파일명/컬럼 제목/토큰 권한을 확인하세요.')
    out = {'_meta': {'generated_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'), 'account': ACCOUNT,
                     'boards': BOARD_IDS, 'files_column': FILES_COLS, **st, 'codes': len(mapping)},
           'map': dict(sorted(mapping.items()))}
    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    dup = sum(1 for e in mapping.values() if e.get('alt'))
    print(f"OK 보드 {st['boards']} · 아이템 {st['items']} · 하위 {st['subitems']} · 파일 {st['files']}(첨부 {st.get('gallery',0)}) → 자재코드 {len(mapping)}개 매핑 (중복코드 {dup}개는 최신 업로드로 연결)")
    if not mapping: print('WARN 매핑 0건: 파일명이 자재코드로 시작하는지, 컬럼 제목이 맞는지 확인하세요.')

if __name__ == '__main__':
    try: main()
    except ApiError as e: raise SystemExit('ERROR monday API: ' + str(e))
