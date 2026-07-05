import json
import boto3
import os
import re
import base64
import io
import logging
import uuid
from datetime import datetime

import pdfplumber

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
ssm = boto3.client('ssm')

DATA_BUCKET = os.environ['DATA_BUCKET']
CUSTOM_KEY  = 'custom_events.json'

CAMP_DATES = [f'2026-07-{d:02d}' for d in range(6, 15)]

TIME_SLOTS = [
    ('09:15', '10:00'),
    ('10:15', '11:00'),
    ('11:15', '12:00'),
    ('13:15', '14:00'),
    ('14:15', '15:00'),
]
# Columns in the 20-col class table where each slot's content lives
SLOT_COLS = [6, 9, 12, 15, 18]


def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
    }


def json_response(status, body):
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json', **cors_headers()},
        'body': json.dumps(body, ensure_ascii=False),
    }


def lambda_handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    path = event.get('rawPath', '/')

    if method == 'OPTIONS':
        return {'statusCode': 200, 'headers': cors_headers(), 'body': ''}

    if method == 'POST' and path == '/upload':
        return handle_upload(event)

    if method == 'POST' and path == '/custom':
        return handle_custom(event)

    return json_response(404, {'error': 'Not found'})


# ── CUSTOM EVENTS ─────────────────────────────────────────────────────────────

def get_custom_events():
    try:
        obj = s3.get_object(Bucket=DATA_BUCKET, Key=CUSTOM_KEY)
        return json.loads(obj['Body'].read()).get('events', [])
    except Exception:
        return []


def save_custom_events(events):
    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=CUSTOM_KEY,
        Body=json.dumps({'events': events}, ensure_ascii=False, indent=2),
        ContentType='application/json',
    )


def handle_custom(event):
    try:
        body_str = event.get('body', '{}') or '{}'
        if event.get('isBase64Encoded'):
            body_str = base64.b64decode(body_str).decode('utf-8')
        body = json.loads(body_str)
    except Exception as e:
        return json_response(400, {'error': f'Invalid request: {e}'})

    password = body.get('password', '')
    action   = body.get('action', '')

    if not password or action not in ('add', 'update', 'delete'):
        return json_response(400, {'error': 'Missing password or invalid action'})

    try:
        param = ssm.get_parameter(Name='/concertocamp/upload_password')
        expected = param['Parameter']['Value']
    except Exception as e:
        logger.error(f'SSM error: {e}')
        return json_response(500, {'error': 'Server configuration error'})

    if password != expected:
        return json_response(401, {'error': 'Invalid password'})

    events = get_custom_events()

    if action == 'add':
        ev = body.get('event', {})
        ev['id'] = str(uuid.uuid4())[:8]
        events.append(ev)

    elif action == 'update':
        ev = body.get('event', {})
        ev_id = ev.get('id')
        events = [e if e.get('id') != ev_id else ev for e in events]

    elif action == 'delete':
        ev_id = body.get('id', '')
        events = [e for e in events if e.get('id') != ev_id]

    try:
        save_custom_events(events)
    except Exception as e:
        logger.error(f'S3 write error: {e}')
        return json_response(500, {'error': 'Failed to save custom events'})

    return json_response(200, {'success': True, 'events': events})


def handle_upload(event):
    try:
        body_str = event.get('body', '{}') or '{}'
        if event.get('isBase64Encoded'):
            body_str = base64.b64decode(body_str).decode('utf-8')
        body = json.loads(body_str)
    except Exception as e:
        return json_response(400, {'error': f'Invalid request: {e}'})

    password = body.get('password', '')
    pdf_b64 = body.get('pdf', '')

    if not password or not pdf_b64:
        return json_response(400, {'error': 'Missing password or pdf field'})

    try:
        param = ssm.get_parameter(Name='/concertocamp/upload_password')
        expected = param['Parameter']['Value']
    except Exception as e:
        logger.error(f'SSM error: {e}')
        return json_response(500, {'error': 'Server configuration error'})

    if password != expected:
        return json_response(401, {'error': 'Invalid password'})

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception as e:
        return json_response(400, {'error': f'Invalid PDF data: {e}'})

    try:
        schedule = parse_pdf(pdf_bytes)
    except Exception as e:
        logger.error(f'PDF parse error: {e}', exc_info=True)
        return json_response(422, {'error': f'PDF parsing failed: {str(e)}'})

    try:
        s3.put_object(
            Bucket=DATA_BUCKET,
            Key='schedule.json',
            Body=json.dumps(schedule, ensure_ascii=False, indent=2),
            ContentType='application/json',
        )
        s3.put_object(
            Bucket=DATA_BUCKET,
            Key='schedule.pdf',
            Body=pdf_bytes,
            ContentType='application/pdf',
        )
    except Exception as e:
        logger.error(f'S3 write error: {e}')
        return json_response(500, {'error': 'Failed to save schedule'})

    student_count = len(schedule.get('students', []))
    event_count = sum(len(s.get('events', [])) for s in schedule.get('students', []))
    logger.info(f'Parsed {student_count} students, {event_count} events')

    return json_response(200, {
        'success': True,
        'students': student_count,
        'events': event_count,
        'generatedAt': schedule.get('generatedAt'),
    })


# ── PDF PARSER ────────────────────────────────────────────────────────────────

def clean(val):
    if val is None:
        return ''
    return str(val).strip()


# ── CHART 1: ROSTER ───────────────────────────────────────────────────────────

def parse_roster(pages):
    students = []
    seen = set()
    for page in pages[:4]:
        text = page.extract_text() or ''
        for line in text.split('\n'):
            # Match "A1 Joanna Ou" or "Group A A2 Corey Pan"
            m = re.search(r'\b([ABCD]\d)\s+([A-Z][^\n\d(]+)', line)
            if m:
                sid = m.group(1)
                name = m.group(2).strip()
                # Skip header words and entries from other charts
                skip_words = ('Student', 'Class', 'Room', 'Teacher', 'Repertoire', 'Group', 'Slot', 'Chart')
                if sid not in seen and name and not any(name.startswith(w) for w in skip_words):
                    students.append({'id': sid, 'name': name, 'group': sid[0], 'events': []})
                    seen.add(sid)
    return students


# ── CHART 2: CLASS SCHEDULE ───────────────────────────────────────────────────

GROUP_CLASS_SLOT_GROUPS = {
    'Conducting':       [None, 'D', 'C', 'B', 'A'],  # index = slot 1-5, None = empty
    'Score-Reading':    [None, 'B', 'A', 'D', 'C'],
    'Piano Literature': [None, 'C', 'B', 'A', 'D'],
}

GROUP_CLASS_TEACHERS = {
    'Conducting': 'Christian Erny',
    'Score-Reading': 'Pawel Markowicz',
    'Piano Literature': 'Prof. Edwin Vanecek',
}

GROUP_CLASS_ROOMS = {
    'Conducting': 'Room 1',
    'Score-Reading': 'Room 24',
    'Piano Literature': 'Room 25',
}

CLASS_DATES = ['2026-07-07', '2026-07-08', '2026-07-11', '2026-07-12']


def parse_classes(pages, by_id, by_group):
    events = []

    # Group classes: identical slot assignments across all 4 dates
    for date in CLASS_DATES:
        for class_name, slot_groups in GROUP_CLASS_SLOT_GROUPS.items():
            room = GROUP_CLASS_ROOMS[class_name]
            teacher = GROUP_CLASS_TEACHERS[class_name]
            for slot_idx, group in enumerate(slot_groups):
                if group is None:
                    continue
                start, end = TIME_SLOTS[slot_idx]
                for sid in by_group.get(group, []):
                    events.append({
                        'student_id': sid,
                        'date': date,
                        'startTime': start,
                        'endTime': end,
                        'type': 'class',
                        'title': class_name,
                        'location': room,
                        'teacher': teacher,
                        'repertoire': None,
                        'notes': None,
                    })

    # Individual lessons & coaching from tables (7/8, 7/12) and text (7/7, 7/11)
    events += parse_individual_from_tables(pages, by_id)
    events += parse_individual_from_text(pages, by_id)

    return events


def parse_individual_from_tables(pages, by_id):
    """Parse private lessons and coaching from 20-col tables (covers 7/8 and 7/12)."""
    events = []
    current_date = None
    # Name lookup fallback handles cases where student name is in slot col but ID
    # is in a sub-row on the next page (Zhen Chen coaching cross-page split).
    name_to_id = {s['name'].lower(): sid for sid, s in by_id.items()}

    # Flatten all rows across pages 3-5 so cross-page sub-rows are reachable.
    all_rows = []
    for page in pages[3:6]:
        for table in page.extract_tables():
            all_rows.extend(table)

    i = 0
    while i < len(all_rows):
        row = [clean(c) for c in all_rows[i]]
        if len(row) < 19:
            i += 1
            continue

        # Update date from col 0
        date_m = re.match(r'(\d+)/(\d+)', row[0])
        if date_m:
            m_val, d_val = int(date_m.group(1)), int(date_m.group(2))
            current_date = f'2026-{m_val:02d}-{d_val:02d}'

        if not current_date:
            i += 1
            continue

        class_name = row[2]
        if class_name not in ('Private Piano Lesson', 'Concerto Coaching'):
            i += 1
            continue

        room = row[4]
        slot1_empty = row[5] in ('@', 'x')
        ev_type = 'coaching' if 'Coaching' in class_name else 'lesson'

        # Teacher from col1 for coaching (e.g. 'Concerto Coaching\n(Zhen Chen)')
        teacher = None
        t_emb = re.search(r'\n\(([^)]+)\)', row[1] if len(row) > 1 else '')
        if t_emb:
            teacher = t_emb.group(1).strip()

        # Collect IDs. Coaching can embed "Name\n(ID)" in the cell just BEFORE the
        # standard slot col (cols 8/11/17 vs 9/12/18).
        slot_ids = [''] * 5
        for si, col_idx in enumerate(SLOT_COLS):
            for check in [col_idx - 1, col_idx]:
                if check < 0 or check >= len(row):
                    continue
                val = row[check]
                emb = re.search(r'\n\(([ABCD]\d)\)', val)
                if emb:
                    slot_ids[si] = emb.group(1)
                    break
                if re.match(r'^\([ABCD]\d\)$', val):
                    slot_ids[si] = val[1:-1]
                    break
                if re.match(r'^[ABCD]\d$', val):
                    slot_ids[si] = val
                    break

        # Look ahead for sub-rows immediately following the main row
        j = i + 1
        while j < len(all_rows):
            sub = [clean(c) for c in all_rows[j]]
            if not sub or len(sub) < 3:
                break

            # Standard sub-row: teacher name in parens at col2
            if sub[2].startswith('(') and not re.match(r'[ABCD]\d', sub[2].lstrip('(')):
                t_m = re.match(r'^\(([^)]+)\)', sub[2])
                if t_m and not teacher:
                    teacher = t_m.group(1).strip()
                for si, col_idx in enumerate(SLOT_COLS):
                    if slot_ids[si]:
                        continue
                    val = sub[col_idx] if col_idx < len(sub) else ''
                    id_m = re.match(r'^\(([ABCD]\d)\)$', val)
                    if id_m:
                        slot_ids[si] = id_m.group(1)
                j += 1
            # Alt sub-row: all header cols empty but IDs at slot positions (Zhen Chen 7/8)
            elif not any(sub[k] for k in range(min(5, len(sub)))):
                if any(len(sub) > c and re.match(r'^\([ABCD]\d\)$', sub[c]) for c in SLOT_COLS):
                    for si, col_idx in enumerate(SLOT_COLS):
                        if slot_ids[si]:
                            continue
                        val = sub[col_idx] if col_idx < len(sub) else ''
                        id_m = re.match(r'^\(([ABCD]\d)\)$', val)
                        if id_m:
                            slot_ids[si] = id_m.group(1)
                    j += 1
                else:
                    break
            else:
                break

        # Name-to-ID fallback: handle cross-page sub-rows (Zhen Chen 7/12) and
        # any other case where a student name is in the slot col but no ID found.
        for si, col_idx in enumerate(SLOT_COLS):
            if slot_ids[si]:
                continue
            name_val = row[col_idx] if col_idx < len(row) else ''
            if name_val and re.match(r'^[A-Z][a-z]', name_val) and ' ' in name_val:
                lid = name_to_id.get(name_val.lower())
                if lid:
                    slot_ids[si] = lid

        # 7/12 coaching is parsed from text (Zhen Chen's row is split across pages
        # in the table, so only parse 7/12 lessons from tables)
        if current_date == '2026-07-12' and ev_type == 'coaching':
            i = j
            continue

        # Create events for found IDs
        for si, sid in enumerate(slot_ids):
            if not sid or sid not in by_id:
                continue
            if slot1_empty and si == 0:
                continue
            start, end = TIME_SLOTS[si]
            events.append({
                'student_id': sid,
                'date': current_date,
                'startTime': start,
                'endTime': end,
                'type': ev_type,
                'title': class_name,
                'location': room,
                'teacher': teacher,
                'repertoire': None,
                'notes': None,
            })

        i = j

    return events


def parse_individual_from_text(pages, by_id):
    """Parse 7/7 and 7/11 individual lessons/coaching from page text."""
    events = []

    # Pages 4-6 combined text, split by "Conducting" which marks each date section
    combined = '\n'.join(
        (pages[idx].extract_text() or '') for idx in range(3, 6)
    )

    # Each date section starts at "Conducting"
    # 4 sections in order: 7/7, 7/8, 7/11, 7/12
    sections = re.split(r'\n(?=Conducting)', combined)

    date_order = CLASS_DATES  # ['2026-07-07','2026-07-08','2026-07-11','2026-07-12']
    section_date_idx = 0

    for section in sections:
        if not section.startswith('Conducting'):
            continue
        if section_date_idx >= len(date_order):
            break
        date = date_order[section_date_idx]
        section_date_idx += 1

        # 7/8 lessons/coaching come from tables
        if date == '2026-07-08':
            continue

        if date == '2026-07-12':
            # 7/12 lessons come from tables; only coaching needed from text
            # (Zhen Chen's coaching main row is split across pages in the table)
            events += _parse_text_section(section, date, by_id, only_coaching=True)
        else:
            events += _parse_text_section(section, date, by_id)

    return events


def _parse_text_section(text, date, by_id, only_coaching=False):
    """Parse individual lesson/coaching entries from a class schedule text section."""
    events = []
    lines = text.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        class_m = re.match(r'^(Private Piano Lesson|Concerto Coaching)\s*(.*)$', line)
        if not class_m:
            i += 1
            continue

        class_name = class_m.group(1)
        if only_coaching and 'Coaching' not in class_name:
            i += 1
            continue

        # Next line: "Room N [@ marker]" possibly with date prefix like "7/11 Room 2"
        room_line = lines[i + 1].strip() if i + 1 < len(lines) else ''
        room_m = re.search(r'(Room\s+\d+)', room_line)
        room = room_m.group(1) if room_m else ''
        skip_slot1 = '@' in room_line

        # ID line: "(Teacher) (ID1) (ID2) ..."
        id_line = lines[i + 2].strip() if i + 2 < len(lines) else ''
        ids = re.findall(r'\(([ABCD]\d)\)', id_line)
        teacher_m = re.match(r'^\(([^)]+)\)', id_line)
        teacher = teacher_m.group(1).strip() if teacher_m else None

        ev_type = 'coaching' if 'Coaching' in class_name else 'lesson'

        for pos, sid in enumerate(ids):
            slot_idx = pos + (1 if skip_slot1 else 0)
            if slot_idx >= len(TIME_SLOTS) or sid not in by_id:
                continue
            start, end = TIME_SLOTS[slot_idx]
            events.append({
                'student_id': sid,
                'date': date,
                'startTime': start,
                'endTime': end,
                'type': ev_type,
                'title': class_name,
                'location': room,
                'teacher': teacher,
                'repertoire': None,
                'notes': None,
            })

        i += 3

    return events


# ── CHART 3: ORCHESTRA REHEARSALS ─────────────────────────────────────────────

def parse_rehearsals(pages):
    events = []
    # Default to July 9 — early entries appear before "7/9" date label in text
    current_date = '2026-07-09'

    for page in pages[6:8]:  # pages 7-8 (index 6-7)
        text = page.extract_text() or ''
        for line in text.split('\n'):
            # Detect date change to 7/10
            if re.search(r'\b7/10\b', line):
                current_date = '2026-07-10'

            # re.search (not match) handles "7/10 HH:MM-..." and "Ehrbarsaal HH:MM-..." prefixes
            entry_m = re.search(
                r'(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s+(.+?)\s+([ABCD]\d)\s+(Mozart.+)',
                line
            )
            if entry_m:
                start = entry_m.group(1)
                end = entry_m.group(2)
                sid = entry_m.group(4)
                repertoire = entry_m.group(5).strip()
                events.append({
                    'student_id': sid,
                    'date': current_date,
                    'startTime': _pad_time(start),
                    'endTime': _pad_time(end),
                    'type': 'rehearsal',
                    'title': 'Orchestra Rehearsal',
                    'location': 'Ehrbarsaal Grand Hall',
                    'teacher': None,
                    'repertoire': repertoire,
                    'notes': 'Approximate time',
                })

    return events


def _pad_time(t):
    parts = t.split(':')
    return f'{int(parts[0]):02d}:{parts[1]}'


# ── CHART 4: SOLO RECITALS ────────────────────────────────────────────────────

def parse_recitals(pages):
    events = []
    # Recital entries for July 8 appear before the "7/8" label in the PDF text
    # (merged cell layout puts metadata below the first few entries). Default to Jul 8.
    current_date = '2026-07-08'
    seen = {}  # (sid, date) -> event index for multi-piece merging
    last_sid = None  # track last matched student for continuation lines

    skip_starts = ('Chart', 'Time', 'Space', 'Student', 'Ehrbarsaal', 'Small', 'Grand',
                   'Main', 'Group', 'Date', 'July', '*Please', 'orchestra', 'short', 'his',
                   'TCC', 'performance', 'concerto')

    for page in pages[7:10]:  # pages 8-10 (index 7-9)
        text = page.extract_text() or ''
        for line in text.split('\n'):
            stripped = line.strip()

            # Date detection: "7/8" or "7/12" anywhere on the line
            date_m = re.search(r'\b7/(8|12)\b', stripped)
            if date_m:
                current_date = f'2026-07-{int(date_m.group(1)):02d}'

            if any(stripped.startswith(s) for s in skip_starts) or not stripped:
                last_sid = None
                continue

            # Recital entry: "Name ID Repertoire..."
            entry_m = re.match(
                r'^([A-Z][A-Za-z\s]+?)\s+([ABCD]\d)\s+(.+)',
                stripped
            )
            if entry_m:
                sid = entry_m.group(2)
                repertoire = entry_m.group(3).strip()

                if sid not in by_id_ref:
                    continue

                key = (sid, current_date)
                if key in seen:
                    events[seen[key]]['repertoire'] += ' / ' + repertoire
                else:
                    seen[key] = len(events)
                    events.append({
                        'student_id': sid,
                        'date': current_date,
                        'startTime': '19:00',
                        'endTime': '21:00',
                        'type': 'recital',
                        'title': 'Solo Recital',
                        'location': 'Ehrbarsaal Small Hall',
                        'teacher': None,
                        'repertoire': repertoire,
                        'notes': None,
                    })
                last_sid = sid
            elif last_sid and re.match(r'^[A-Z]', stripped):
                # Continuation repertoire line (second piece, no student ID)
                key = (last_sid, current_date)
                if key in seen:
                    events[seen[key]]['repertoire'] += ' / ' + stripped
            else:
                last_sid = None

    return events


# Global reference for by_id (set during parse_pdf)
by_id_ref = {}


# ── CHART 5: CAPSTONE CONCERTS ────────────────────────────────────────────────

CONCERT_INFO = {
    '1': ('10:00', '12:00', 'Ehrbarsaal Grand Hall'),
    '2': ('14:00', '16:00', 'Ehrbarsaal Main Hall'),
    '3': ('18:00', '20:00', 'Ehrbarsaal Main Hall'),
}


def parse_concerts(pages):
    events = []
    # Concert entries 1-3 appear in the PDF text BEFORE the "Concert N" label (merged
    # cell layout). Use order-number reset (1 < last_order) to detect concert changes.
    current_concert = '1'
    last_order = 0

    text = pages[10].extract_text() or ''  # page 11 (index 10)
    for line in text.split('\n'):
        # Concert entry: "N Mozart... Name ID"
        entry_m = re.match(
            r'^(\d+)\s+(Mozart.+)\s([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s([ABCD]\d)\s*$',
            line.strip()
        )
        if entry_m:
            order = int(entry_m.group(1))
            if order < last_order:
                current_concert = str(int(current_concert) + 1)
            last_order = order
            sid = entry_m.group(4)
            repertoire = entry_m.group(2).strip()
            start, end, location = CONCERT_INFO.get(current_concert, ('10:00', '12:00', 'Ehrbarsaal'))
            events.append({
                'student_id': sid,
                'date': '2026-07-14',
                'startTime': start,
                'endTime': end,
                'type': 'concert',
                'title': f'Capstone Concert {current_concert}',
                'location': location,
                'teacher': None,
                'repertoire': repertoire,
                'notes': None,
            })

    return events


# ── CHART 6: PRACTICE ROOMS ───────────────────────────────────────────────────

DATE_MAP = {
    'July 6': '2026-07-06',
    'July 7': '2026-07-07',
    'July 8': '2026-07-08',
    'July 9': '2026-07-09',
    'July 10': '2026-07-10',
    'July 11': '2026-07-11',
    'July 12': '2026-07-12',
    'July 13': '2026-07-13',
    'July 14': '2026-07-14',
}


def parse_practice_rooms(pages):
    if len(pages) < 13:
        return []

    events = []
    page12_text = pages[11].extract_text() or ''
    page13_text = pages[12].extract_text() or ''

    rooms_p12, slots_p12 = _parse_practice_page(page12_text)
    rooms_p13, slots_p13 = _parse_practice_page(page13_text)

    all_keys = set(slots_p12.keys()) | set(slots_p13.keys())
    for key in sorted(all_keys):
        date, time_range = key
        parts = time_range.split('-')
        if len(parts) != 2:
            continue
        start, end = _pad_time(parts[0]), _pad_time(parts[1])

        combined = {}
        for room, sid in (slots_p12.get(key) or {}).items():
            combined[room] = sid
        for room, sid in (slots_p13.get(key) or {}).items():
            combined[room] = sid

        for room, sid in combined.items():
            if sid and re.match(r'[ABCD]\d', sid):
                events.append({
                    'student_id': sid,
                    'date': date,
                    'startTime': start,
                    'endTime': end,
                    'type': 'practice',
                    'title': 'Practice Room',
                    'location': room,
                    'teacher': None,
                    'repertoire': None,
                    'notes': None,
                })

    return events


def _parse_practice_page(text):
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    rooms = []
    slot_dict = {}
    current_date = None

    for line in lines:
        # Room header line
        if re.match(r'^Room \d', line):
            rooms = re.findall(r'Room (\d+)', line)
            continue

        # Data row: optional "July N[*]" then "HH:MM-HH:MM" then values
        m = re.match(
            r'^(?:(July\s+\d+)\*?\s+)?(\d{1,2}:\d{2}-\d{2}:\d{2})\s+(.+)',
            line
        )
        if m:
            if m.group(1):
                date_str = m.group(1).strip()
                current_date = DATE_MAP.get(date_str)
            time_range = m.group(2)
            values_str = m.group(3)

            if not current_date or not rooms:
                continue

            tokens = [t for t in values_str.split() if t != 'continue']

            key = (current_date, time_range)
            room_assignments = {}
            for idx, room in enumerate(rooms):
                if idx < len(tokens):
                    val = tokens[idx]
                    room_assignments[f'Room {room}'] = val if val != 'x' and val != '@' else None
            slot_dict[key] = room_assignments

    return rooms, slot_dict


# ── override parse_pdf to pass by_id to recitals ─────────────────────────────

def parse_pdf(pdf_bytes):
    global by_id_ref
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = pdf.pages
        if len(pages) < 11:
            raise ValueError(f'Expected at least 11 pages, got {len(pages)}')

        students = parse_roster(pages)
        logger.info(f'Roster: {len(students)} students')

        by_id = {s['id']: s for s in students}
        by_id_ref = by_id  # make available to parse_recitals

        by_group = {}
        for s in students:
            by_group.setdefault(s['group'], []).append(s['id'])

        all_events = []
        all_events += parse_classes(pages, by_id, by_group)
        all_events += parse_rehearsals(pages)
        all_events += parse_recitals(pages)
        all_events += parse_concerts(pages)
        all_events += parse_practice_rooms(pages)

        for ev in all_events:
            sid = ev.pop('student_id', None)
            if sid and sid in by_id:
                by_id[sid].setdefault('events', []).append(ev)

        for s in students:
            s['events'] = sorted(
                s.get('events', []),
                key=lambda e: (e.get('date', ''), e.get('startTime', ''))
            )

        return {
            'generatedAt': datetime.utcnow().isoformat() + 'Z',
            'campDates': CAMP_DATES,
            'students': students,
        }
