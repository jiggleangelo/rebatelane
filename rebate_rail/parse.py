import csv, io, json, re
from decimal import Decimal, InvalidOperation

MAX_ROWS, MAX_BYTES, MAX_CELL = 5000, 1024 * 1024, 256
ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
PERIOD_RE = re.compile(r'^[0-9]{4}-(0[1-9]|1[0-2])$')
KEY_RE = re.compile(r'^(sk_|rk_|pk_)', re.I)
OPTIONAL = ('billing_customer_id', 'top_fingerprint_share', 'peak_calls_per_min', 'error_rate', 'prior_period_calls', 'account_age_days')
csv.field_size_limit(MAX_BYTES)

class Grants(list):
    """Sanitized grants plus evidence about the completeness of their source."""
    def __init__(self, values=(), complete=True, pages=1):
        super().__init__(values); self.complete, self.pages = complete, pages

def decode_upload(value):
    if not isinstance(value, bytes): return str(value).lstrip('\ufeff')
    if len(value) > MAX_BYTES: raise ValueError('file exceeds 1 MB')
    if value.startswith((b'\xff\xfe', b'\xfe\xff')): return value.decode('utf-16')
    try: return value.decode('utf-8-sig')
    except UnicodeDecodeError: return value.decode('cp1252')
def _text(value):
    text = decode_upload(value)
    if len(text.encode('utf-8')) > MAX_BYTES: raise ValueError('file exceeds 1 MB after decoding')
    return text

def safe_id(value, label='ID', allow_blank=False):
    value = str(value or '').strip()
    if allow_blank and not value: return ''
    if '@' in value: raise ValueError(f'{label} looks like an email; use opaque IDs')
    if KEY_RE.match(value): raise ValueError(f'{label} looks like an API key')
    if not ID_RE.fullmatch(value): raise ValueError(f'invalid {label}')
    return value

def _cell(value):
    value = '' if value is None else str(value).strip()
    if len(value) > MAX_CELL: raise ValueError('cell exceeds 256 characters')
    return value
def _integer(value, label, minimum=0):
    value = _cell(value)
    if not re.fullmatch(r'[0-9]{1,16}', value): raise ValueError(f'{label} must be a nonnegative integer of at most 16 digits')
    result = int(value)
    if result < minimum: raise ValueError(f'{label} is out of range')
    return result
def _ratio(value, label):
    try: result = float(_cell(value))
    except ValueError as exc: raise ValueError(f'{label} must be a number') from exc
    if not 0 <= result <= 1: raise ValueError(f'{label} must be between 0 and 1')
    return result

def _decimal_currency(value):
    value = _cell(value)
    if value.startswith('$'): value = value[1:].strip()
    if ',' in value:
        if not re.fullmatch(r'[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?', value): raise ValueError('spend has invalid thousands separators')
        value = value.replace(',', '')
    try: amount = Decimal(value)
    except InvalidOperation as exc: raise ValueError('spend must be decimal currency') from exc
    if not amount.is_finite() or not 0 <= amount <= 10**14 or amount.as_tuple().exponent < -2: raise ValueError('spend must be nonnegative, at most 100000000000000, with at most 2 decimal places')
    return int(amount * 100)
def _money(row): return _integer(row['spend_cents'], 'spend_cents') if row.get('spend_cents') else _decimal_currency(row.get('spend'))

def _csv(text):
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''), strict=True); headers = reader.fieldnames or []
        if len(headers) != len(set(headers)): raise ValueError('duplicate CSV column names')
        yield headers
        for index, row in enumerate(reader, 1):
            if index > MAX_ROWS: raise ValueError('CSV has more than 5,000 rows')
            yield row
    except csv.Error as exc: raise ValueError('invalid CSV: ' + str(exc)) from exc

def parse_usage(data):
    source = _csv(_text(data)); headers = next(source)
    if not {'account_id', 'period', 'calls'}.issubset(headers) or not {'spend', 'spend_cents'}.intersection(headers): raise ValueError('required columns: account_id, period, calls, and spend or spend_cents')
    rows, dropped = [], []; retained = set(OPTIONAL) | {'account_id', 'period', 'calls', 'spend', 'spend_cents'}
    for line, raw in enumerate(source, 2):
        try:
            if None in raw: raise ValueError('row has extra columns')
            raw = {key: _cell(value) for key, value in raw.items() if key in retained}; period = raw['period']
            if not PERIOD_RE.fullmatch(period) or not 1 <= int(period[:4]) <= 9989: raise ValueError('period must be YYYY-MM with year 0001–9989')
            item = {'account_id': safe_id(raw['account_id'], 'account_id'), 'period': period, 'calls': _integer(raw['calls'], 'calls'), 'spend_cents': _money(raw)}
            customer = safe_id(raw.get('billing_customer_id'), 'billing_customer_id', True)
            if customer and not re.fullmatch(r'cus_[A-Za-z0-9]+', customer): raise ValueError('billing_customer_id must start with cus_ and contain letters or digits')
            item['billing_customer_id'] = customer
            for name in OPTIONAL[1:]:
                value = raw.get(name, ''); item[name] = None if value == '' else (_ratio(value, name) if name in ('top_fingerprint_share', 'error_rate') else _integer(value, name))
            rows.append(item)
        except ValueError as exc: dropped.append({'line': line, 'reason': str(exc)})
    if not rows: raise ValueError('no valid usage rows')
    if len({row['period'] for row in rows}) != 1: raise ValueError('one period is allowed per run')
    return rows, dropped

def _metadata_reference(item):
    direct = item.get('rr_ref') or item.get('metadata[rr_ref]') or item.get('metadata.rr_ref')
    if direct: return direct
    metadata = item.get('metadata')
    if isinstance(metadata, str) and metadata.strip():
        try: metadata = json.loads(metadata)
        except (TypeError, ValueError): return ''
    return metadata.get('rr_ref', '') if isinstance(metadata, dict) else ''
def _grant(item):
    if not isinstance(item, dict): raise ValueError('each grant must be an object')
    if None in item: raise ValueError('row has extra columns')
    amount = item.get('amount'); monetary = amount.get('monetary', {}) if isinstance(amount, dict) else {}
    if not isinstance(monetary, dict): raise ValueError('invalid monetary amount')
    currency = _cell(item.get('currency', monetary.get('currency', 'usd'))).lower()
    if not re.fullmatch(r'[a-z]{3}', currency): raise ValueError('currency must be a three-letter code')
    return {'id': safe_id(item.get('id'), 'grant id'), 'customer': safe_id(item.get('customer'), 'customer', True), 'amount_cents': _integer(item.get('amount_cents', monetary.get('value', '')), 'amount_cents'), 'currency': currency, 'expires_at': _integer(item.get('expires_at') or 0, 'expires_at'), 'rr_ref': safe_id(_metadata_reference(item), 'rr_ref', True)}

def _json_grants(text):
    decoder, documents, offset = json.JSONDecoder(), [], 0
    try:
        while offset < len(text):
            value, offset = decoder.raw_decode(text, offset); documents.append(value)
            while offset < len(text) and text[offset].isspace(): offset += 1
            if len(documents) > MAX_ROWS: raise ValueError('too many pages')
    except (ValueError, RecursionError) as exc: raise ValueError('invalid grants JSON') from exc
    if len(documents) == 1 and isinstance(documents[0], list):
        values = documents[0]
        if not values or not all(isinstance(x, dict) and isinstance(x.get('data'), list) for x in values): return values, True, 1
        documents = values
    values, flags = [], []
    for document in documents:
        if not isinstance(document, dict) or not isinstance(document.get('data'), list): raise ValueError('use a grant array or Stripe list pages')
        flag = document.get('has_more', False)
        if type(flag) is not bool: raise ValueError('has_more must be a boolean')
        flags.append(flag); values.extend(document['data'])
        if len(values) > MAX_ROWS: raise ValueError('at most 5,000 grants across all pages')
    # Multiple pages must end with an explicit terminal page. The UI also asks
    # the owner to attest that all pages, without filters or omissions, are here.
    complete = not flags[-1] and (len(documents) == 1 or (documents[-1].get('has_more') is False and all(flags[:-1])))
    return values, complete, len(documents)

def parse_grants(data, dropped=None):
    dropped = [] if dropped is None else dropped; text = _text(data).strip()
    if not text: return Grants()
    first, complete, pages = 1, True, 1
    if text[0] in '[{':
        values, complete, pages = _json_grants(text)
        if not isinstance(values, list) or len(values) > MAX_ROWS: raise ValueError('expected at most 5,000 grants')
    else:
        source = _csv(text)
        if not {'id', 'customer', 'amount_cents', 'currency', 'expires_at'}.issubset(next(source)): raise ValueError('missing grants CSV columns')
        values, first = list(source), 2
    grants = Grants(complete=complete, pages=pages)
    for line, item in enumerate(values, first):
        try: grants.append(_grant(item))
        except ValueError as exc: dropped.append({'line': line, 'reason': str(exc)})
    if values and not grants: raise ValueError('no valid grants; correct the export before reconciling')
    grants.complete = grants.complete and not dropped
    return grants

def parse_references(data):
    text = _text(data).strip()
    if not text: raise ValueError('provide at least one reference')
    lines = text.splitlines()
    if lines[0].startswith('# '): text = '\n'.join(lines[1:])
    names = {'reference', 'rr_ref', 'metadata[rr_ref]', 'Idempotency-Key'}
    try:
        rows = csv.reader(io.StringIO(text, newline=''), strict=True); first = next(rows, [])
        columns = [index for index, name in enumerate(first) if name in names]; candidates = []
        if columns:
            for row in rows:
                if not row: continue
                if len(row) != len(first) or len({row[index].strip() for index in columns}) != 1: raise ValueError('reference CSV has inconsistent columns')
                candidates.append(row[columns[0]])
                if len(candidates) > MAX_ROWS: raise ValueError('at most 5,000 references are allowed')
        else:
            candidates.extend(first)
            for row in rows:
                candidates.extend(row)
                if len(candidates) > MAX_ROWS: raise ValueError('at most 5,000 references are allowed')
    except csv.Error as exc: raise ValueError('invalid references CSV') from exc
    if not candidates or len(candidates) > MAX_ROWS: raise ValueError('provide between 1 and 5,000 references')
    refs = []
    for value in candidates:
        ref = safe_id(value, 'reference')
        if not ref.startswith('ci_') or len(ref) == 3: raise ValueError('references must start with ci_')
        refs.append(ref)
    return list(dict.fromkeys(refs))
