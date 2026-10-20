#!/usr/bin/env python3
"""도안 파일 메일 발송 (GitHub Actions용)

자재코드 목록을 받아 monday_map.json 에서 각 코드의 최신 .ai 도안 파일을 찾고, 먼데이 API로 파일을 내려받아
받는 주소로 첨부 메일을 보낸다. 비밀은 환경변수로만 받는다(저장소에 두지 않음).
  MONDAY_TOKEN      먼데이 API 토큰 (기존 시크릿)
  MAIL_USER         보내는 메일 주소 (예: Gmail)
  MAIL_PASSWORD     앱 비밀번호
  SMTP_HOST/PORT    기본 smtp.gmail.com:587
사용: python send_design_mail.py --to 받는주소 --codes 3PK220460,3BS220470 [--subject 제목] [--ext ai]
"""
import argparse, json, os, re, smtplib, sys, urllib.request
from email.message import EmailMessage

API = 'https://api.monday.com/v2'
MAX_TOTAL_MB = 20   # Gmail 첨부 한도(25MB)보다 여유 있게


def pick_assets(mapping, codes, ext='ai'):
    """코드별 최신 .ai 파일 (asset id). monday_map.json 의 files 는 최신 날짜 순."""
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
            out.append({'code': code.upper(), 'name': files[0]['name'], 'asset_id': aid, 'updated': files[0].get('updated') or ''})
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


def download_assets(token, picks):
    ids = [p['asset_id'] for p in picks]
    data = gql(token, 'query($ids:[ID!]){ assets(ids:$ids){ id name file_size public_url } }', {'ids': ids})
    by_id = {a['id']: a for a in data.get('assets') or []}
    files = []
    for p in picks:
        a = by_id.get(p['asset_id'])
        if not a or not a.get('public_url'):
            print(f"WARN {p['code']}: 파일 {p['name']} 다운로드 주소 없음 — 건너뜀"); continue
        with urllib.request.urlopen(a['public_url'], timeout=120) as r:
            blob = r.read()
        if not blob.startswith(b'%PDF') and not blob.startswith(b'%!PS'):
            print(f"WARN {p['code']}: {a['name']} 내용이 Illustrator 파일 형식이 아님({blob[:8]!r}) — 그래도 첨부")
        files.append((a['name'], blob, p['code']))
        print(f"OK {p['code']}: {a['name']} ({len(blob) / 1024 / 1024:.1f} MB, 업로드 {p['updated']})")
    return files


def build_message(sender, to, subject, files, missing):
    msg = EmailMessage()
    msg['From'] = sender; msg['To'] = to; msg['Subject'] = subject
    lines = ['패키지 도안 파일을 보내드립니다.', '', '첨부:']
    lines += [f' - {code}: {name} ({len(blob) / 1024 / 1024:.1f} MB)' for name, blob, code in files]
    if missing:
        lines += ['', '도안 파일을 찾지 못한 자재: ' + ', '.join(missing)]
    lines += ['', '(BOM 검색 도우미 자동 발송)']
    msg.set_content('\n'.join(lines))   # 본문 먼저, 첨부는 그 다음
    for name, blob, _ in files:
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
    if not token: sys.exit('ERROR MONDAY_TOKEN 없음')
    if not a.dry_run and not (sender and pw): sys.exit('ERROR MAIL_USER / MAIL_PASSWORD 시크릿이 없습니다')
    codes = [c.strip() for c in re.split(r'[\s,]+', a.codes) if c.strip()]
    mapping = json.load(open(a.map, encoding='utf-8')).get('map') or {}
    picks, missing = pick_assets(mapping, codes, a.ext)
    if missing: print('WARN 도안 없음: ' + ', '.join(missing))
    if not picks: sys.exit('ERROR 보낼 파일이 없습니다')
    files = download_assets(token, picks)
    total = sum(len(b) for _, b, _ in files) / 1024 / 1024
    if total > MAX_TOTAL_MB: sys.exit(f'ERROR 첨부 합계 {total:.1f} MB 가 한도 {MAX_TOTAL_MB} MB 를 넘습니다. 나눠서 보내세요.')
    subject = a.subject or f"[도안] {', '.join(p['code'] for p in picks)}"
    msg = build_message(sender or 'dry-run@example.com', a.to, subject, files, missing)
    if a.dry_run:
        print(f'DRY-RUN: {a.to} 에게 {len(files)}개 파일 {total:.1f} MB 보낼 준비 완료 (제목: {subject})'); return
    host = os.environ.get('SMTP_HOST', 'smtp.gmail.com'); port = int(os.environ.get('SMTP_PORT', '587'))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls(); s.login(sender, pw); s.send_message(msg)
    print(f'OK 발송: {a.to} ← {sender} · 파일 {len(files)}개 · {total:.1f} MB · 제목 "{subject}"')


if __name__ == '__main__':
    main()
