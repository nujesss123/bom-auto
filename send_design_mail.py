#!/usr/bin/env python3
"""도안 파일 메일 발송 (GitHub Actions용)

자재코드 목록을 받아 monday_map.json 에서 각 코드의 최신 .ai 도안 파일을 찾고, 먼데이 API로 파일을 내려받아
받는 주소로 첨부 메일(영문)을 보낸다. 문의처(담당자)는 먼데이에서 읽는다: 파일이 있는 아이템의 '담당' 열 사람들
+ 파일 업로더. 비밀은 환경변수로만 받는다(저장소에 두지 않음).
  MONDAY_TOKEN      먼데이 API 토큰 (기존 시크릿)
  MAIL_USER         보내는 메일 주소 (예: Gmail)
  MAIL_PASSWORD     앱 비밀번호
  SMTP_HOST/PORT    기본 smtp.gmail.com:587
사용: python send_design_mail.py --to 받는주소 --codes 3PK220460,3BS220470 [--subject 제목] [--ext ai] [--dry-run]
"""
import argparse, json, os, re, smtplib, sys, urllib.request
from email.message import EmailMessage

API = 'https://api.monday.com/v2'
MAX_TOTAL_MB = 20          # Gmail 첨부 한도(25MB)보다 여유 있게
CONTACT_TITLE = '담당'     # 이 글자가 든 열 제목(담당자, 패키지 담당 등)의 사람을 문의처로
SIGNATURE = os.environ.get('MAIL_SIGNATURE', 'Spigen Package Design Team')


def pick_assets(mapping, codes, ext='ai'):
    """코드별 최신 .ai 파일 (asset id, 아이템 id). monday_map.json 의 files 는 최신 날짜 순."""
    out, missing = [], []
    for code in codes:
        e = (mapping or {}).get(code.upper()) or {}
        files = [f for f in e.get('files') or [] if str(f.get('name', '')).lower().endswith('.' + ext.lower())]
        files.sort(key=lambda f: f.get('updated') or '', reverse=True)
        aid = None
        if files:
            m = re.search(r'asset_id=(\d+)', files[0].get('url') or '')
            aid = m.group(1) if m else None
        if aid:
            out.append({'code': code.upper(), 'name': files[0]['name'], 'asset_id': aid,
                        'updated': files[0].get('updated') or '', 'item_id': str(files[0].get('item_id') or '')})
        else:
            missing.append(code.upper())
    return out, missing


def gql(token, query, variables):
    body = json.dumps({'query': query, 'variables': variables}).encode('utf-8')
    req = urllib.request.Request(API, data=body, headers={'Authorization': token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read().decode('utf-8'))
    if res.get('errors'):
        raise SystemExit('ERROR monday API: ' + json.dumps(res['errors'], ensure_ascii=False)[:300])
    return res['data']


Q_ASSETS = 'query($ids:[ID!]!){ assets(ids:$ids){ id name file_size public_url uploaded_by { id name email } } }'
Q_ITEMS = ('query($ids:[ID!]){ items(ids:$ids){ id column_values(types:[people, mirror]){ column { title } '
           '... on PeopleValue { persons_and_teams { id kind } } ... on MirrorValue { display_value } } } }')
Q_USERS = 'query($ids:[ID!]){ users(ids:$ids, limit: 200){ id name email } }'
Q_ALL_USERS = 'query{ users(limit: 1000){ id name email } }'


def split_name(full):
    """'황선혜/브랜드디자인팀/HQ' -> ('황선혜', '브랜드디자인팀')"""
    parts = [p.strip() for p in str(full or '').split('/') if p.strip()]
    return (parts[0] if parts else ''), (parts[1] if len(parts) > 1 else '')


def contacts_from(items, assets, users_by_id, users_by_name):
    """문의처 목록 [{name, team, email}] — 아이템의 '담당' 열 사람(people: id, mirror: 이름) + 파일 업로더. 이메일 기준 중복 제거."""
    found = []
    for it in items or []:
        for cv in it.get('column_values') or []:
            title = ((cv.get('column') or {}).get('title') or '')
            if CONTACT_TITLE not in title: continue
            for p in cv.get('persons_and_teams') or []:
                if p.get('kind') == 'person' and str(p.get('id')) in users_by_id:
                    found.append(users_by_id[str(p['id'])])
            for nm in re.split(r'\s*,\s*', cv.get('display_value') or ''):
                if nm and nm in users_by_name:
                    found.append(users_by_name[nm])
    for a in assets or []:
        u = a.get('uploaded_by') or {}
        if u.get('email'):
            found.append({'name': u.get('name') or '', 'email': u['email']})
    out, seen = [], set()
    for u in found:
        em = (u.get('email') or '').strip().lower()
        if not em or em in seen: continue
        seen.add(em)
        nm, team = split_name(u.get('name'))
        out.append({'name': nm, 'team': team, 'email': em})
    return out


def fetch_contacts(token, picks, assets):
    item_ids = sorted({p['item_id'] for p in picks if p.get('item_id')})
    items = (gql(token, Q_ITEMS, {'ids': item_ids}).get('items') or []) if item_ids else []
    ids, names = set(), set()
    for it in items:
        for cv in it.get('column_values') or []:
            if CONTACT_TITLE not in ((cv.get('column') or {}).get('title') or ''): continue
            ids.update(str(p['id']) for p in cv.get('persons_and_teams') or [] if p.get('kind') == 'person')
            names.update(n for n in re.split(r'\s*,\s*', cv.get('display_value') or '') if n)
    users_by_id, users_by_name = {}, {}
    if ids:
        for u in gql(token, Q_USERS, {'ids': sorted(ids)}).get('users') or []: users_by_id[str(u['id'])] = u
    if names:
        for u in gql(token, Q_ALL_USERS, {}).get('users') or []: users_by_name[u.get('name') or ''] = u
    return contacts_from(items, assets, users_by_id, users_by_name)


def download_assets(token, picks):
    data = gql(token, Q_ASSETS, {'ids': [p['asset_id'] for p in picks]})
    by_id = {a['id']: a for a in data.get('assets') or []}
    files, used = [], []
    for p in picks:
        a = by_id.get(p['asset_id'])
        if not a or not a.get('public_url'):
            print(f"WARN {p['code']}: 파일 {p['name']} 다운로드 주소 없음 — 건너뜀"); continue
        with urllib.request.urlopen(a['public_url'], timeout=120) as r:
            blob = r.read()
        if not blob.startswith(b'%PDF') and not blob.startswith(b'%!PS'):
            print(f"WARN {p['code']}: {a['name']} 내용이 Illustrator 파일 형식이 아님({blob[:8]!r}) — 그래도 첨부")
        files.append((a['name'], blob, p['code'], p.get('updated') or '')); used.append(a)
        print(f"OK {p['code']}: {a['name']} ({len(blob) / 1024 / 1024:.1f} MB, 업로드 {p['updated']})")
    return files, used


def build_message(sender, to, subject, files, missing, contacts):
    msg = EmailMessage()
    msg['From'] = sender; msg['To'] = to; msg['Subject'] = subject
    if contacts:
        msg['Reply-To'] = ', '.join(c['email'] for c in contacts)   # 답장은 담당자에게
    lines = ['Hello,', '',
             'Please find the attached package design file(s) (Adobe Illustrator, .ai).', '',
             'Attached files:']
    lines += [f' - {code}: {name} ({len(blob) / 1024 / 1024:.1f} MB, uploaded {upd})' for name, blob, code, upd in files]
    if missing:
        lines += ['', 'No design file was found for: ' + ', '.join(missing) + '. Please contact us if you need these files.']
    lines += ['', 'If you have any questions, please contact:']
    if contacts:
        lines += [f" - {c['name']}{' (' + c['team'] + ')' if c['team'] else ''} <{c['email']}>" for c in contacts]
    else:
        lines += [f' - {sender}']
    lines += ['', 'Best regards,', SIGNATURE, '', '(This email was sent automatically by the BOM helper system.)']
    msg.set_content('\n'.join(lines))   # 본문 먼저, 첨부는 그 다음
    for name, blob, _, _ in files:
        msg.add_attachment(blob, maintype='application', subtype='postscript', filename=name)
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--to', required=True); ap.add_argument('--codes', required=True)
    ap.add_argument('--subject', default=''); ap.add_argument('--ext', default='ai')
    ap.add_argument('--map', default='monday_map.json'); ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    token = os.environ.get('MONDAY_TOKEN', '').strip()
    sender = os.environ.get('MAIL_USER', '').strip(); pw = os.environ.get('MAIL_PASSWORD', '').strip().replace(' ', '')
    print('시크릿 확인: ' + ' · '.join(f"{k}={'있음' if v else '없음'}" for k, v in [('MONDAY_TOKEN', token), ('MAIL_USER', sender), ('MAIL_PASSWORD', pw)]))
    if not token: sys.exit('ERROR MONDAY_TOKEN 시크릿이 없습니다')
    if not a.dry_run and not (sender and pw):
        sys.exit('ERROR MAIL_USER / MAIL_PASSWORD 시크릿이 없습니다 — Settings > Secrets and variables > Actions 에서 이름을 정확히 MAIL_USER, MAIL_PASSWORD 로 추가하세요')
    print(f'입력: 받는 주소 {a.to} · 자재코드 {a.codes} · dry_run {a.dry_run}')
    codes = [c.strip() for c in re.split(r'[\s,]+', a.codes) if c.strip()]
    mapping = json.load(open(a.map, encoding='utf-8')).get('map') or {}
    picks, missing = pick_assets(mapping, codes, a.ext)
    if missing: print('WARN 도안 없음: ' + ', '.join(missing))
    if not picks: sys.exit('ERROR 보낼 파일이 없습니다')
    files, used_assets = download_assets(token, picks)
    total = sum(len(b) for _, b, _, _ in files) / 1024 / 1024
    if total > MAX_TOTAL_MB: sys.exit(f'ERROR 첨부 합계 {total:.1f} MB 가 한도 {MAX_TOTAL_MB} MB 를 넘습니다. 나눠서 보내세요.')
    contacts = fetch_contacts(token, picks, used_assets)
    print('문의처: ' + (', '.join(f"{c['name']} <{c['email']}>" for c in contacts) or '(없음 → 보내는 주소 안내)'))
    subject = a.subject or f"[Spigen] Package design files: {', '.join(p['code'] for p in picks)}"
    msg = build_message(sender or 'dry-run@example.com', a.to, subject, files, missing, contacts)
    if a.dry_run:
        print(f'DRY-RUN: {a.to} 에게 {len(files)}개 파일 {total:.1f} MB 보낼 준비 완료 (제목: {subject})')
        print('--- 본문 ---'); print(msg.get_body().get_content()); return
    host = os.environ.get('SMTP_HOST', 'smtp.gmail.com'); port = int(os.environ.get('SMTP_PORT', '587'))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls(); s.login(sender, pw); s.send_message(msg)
    print(f'OK 발송: {a.to} ← {sender} · 파일 {len(files)}개 · {total:.1f} MB · 제목 "{subject}"')


if __name__ == '__main__':
    main()
