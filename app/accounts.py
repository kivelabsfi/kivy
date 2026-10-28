"""Kivy accounts: chat history belongs to the wallet that signed in (the owner, 2026-10-15).

Sign-in: the server hands out a one-time message, the user's wallet signs it (a free
personal-message signature, not a transaction), and we check the signature belongs to
the claimed address before handing out a signed cookie. Each address's chats live in
their own file, named by a hash of the address; the server only ever opens the file
for the address inside a valid cookie, so nobody can read anyone else's chats.
(Google/zkLogin sign-in was removed the same day; verify_google is kept unused.)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa, utils as ec_utils

HOME = Path(__file__).resolve().parent
DATA = HOME / 'data'
CHATS = DATA / 'chats'
USER_COOKIE, USER_TTL = 'kivy_user', 7 * 24 * 3600
MAX_CHATS, MAX_TURNS, MAX_CHAT_BYTES = 40, 200, 150_000
ISSUERS = ('accounts.google.com', 'https://accounts.google.com')
CERTS = 'https://www.googleapis.com/oauth2/v3/certs'
ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
_lock = threading.Lock()
_jwks = {'at': 0, 'keys': {}}
LOGOUTS = DATA / 'logouts.json'   # email -> time of the last Log out: every login pass issued before it stops working (red-team low #2)
_logouts = None


def _key():
    p = DATA / '.user_key'
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32)); os.chmod(p, 0o600)
    return p.read_bytes()


def _b64d(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _client_id():
    try:
        return json.load(open(HOME / 'zklogin.json')).get('google_client_id') or ''
    except Exception:
        return ''


def _google_key(kid):
    if kid not in _jwks['keys'] or time.time() - _jwks['at'] > 3600:
        with urllib.request.urlopen(CERTS, timeout=10) as r:
            keys = json.load(r)['keys']
        _jwks['keys'] = {k['kid']: rsa.RSAPublicNumbers(int.from_bytes(_b64d(k['e']), 'big'),
                                                        int.from_bytes(_b64d(k['n']), 'big')).public_key()
                         for k in keys if k.get('kty') == 'RSA'}
        _jwks['at'] = time.time()
    return _jwks['keys'].get(kid)


def verify_google(jwt):
    """Return the verified, lower-cased email in a Google ID token, or raise ValueError."""
    try:
        h, p, s = jwt.split('.')
        head, claims = json.loads(_b64d(h)), json.loads(_b64d(p))
    except Exception:
        raise ValueError('bad token')
    if head.get('alg') != 'RS256':
        raise ValueError('bad alg')
    key = _google_key(head.get('kid'))
    if not key:
        raise ValueError('unknown key')
    try:
        key.verify(_b64d(s), (h + '.' + p).encode(), padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise ValueError('bad signature')
    now = time.time()
    if claims.get('iss') not in ISSUERS:
        raise ValueError('bad issuer')
    if not _client_id() or claims.get('aud') != _client_id():
        raise ValueError('bad audience')
    if not (float(claims.get('exp', 0)) > now - 60 and float(claims.get('iat', 0)) < now + 300):
        raise ValueError('expired')
    email = str(claims.get('email') or '').strip().lower()
    if not email or claims.get('email_verified') not in (True, 'true'):
        raise ValueError('no verified email')
    return email


def make_cookie(email):
    body = base64.urlsafe_b64encode(email.encode()).decode().rstrip('=') + '.' + str(int(time.time()) + USER_TTL)
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return body + '.' + sig


def _logout_at(email):
    global _logouts
    with _lock:
        if _logouts is None:
            try:
                _logouts = json.loads(LOGOUTS.read_text())
            except Exception:
                _logouts = {}
        return _logouts.get(email, 0)


def log_out_everywhere(email):
    global _logouts
    _logout_at(email)
    with _lock:
        _logouts[email] = now = int(time.time())
        _logouts = {k: v for k, v in _logouts.items() if v > now - USER_TTL}   # older than any live pass: nothing left to cancel
        tmp = LOGOUTS.with_suffix('.tmp')
        tmp.write_text(json.dumps(_logouts)); os.chmod(tmp, 0o600); tmp.replace(LOGOUTS)


def email_from_cookie(header):
    for part in (header or '').split(';'):
        k, _, v = part.strip().partition('=')
        if k != USER_COOKIE:
            continue
        try:
            b, exp, sig = v.rsplit('.', 2)
            if not hmac.compare_digest(sig, hmac.new(_key(), (b + '.' + exp).encode(), hashlib.sha256).hexdigest()):
                return None
            if int(exp) < time.time():
                return None
            email = _b64d(b).decode()
            if int(exp) - USER_TTL < _logout_at(email):    # issued before this account's last Log out
                return None
            return email
        except Exception:
            return None
    return None


def _file(email):
    return CHATS / (hashlib.sha256(('kivy:' + email).encode()).hexdigest() + '.json')


# Chats are encrypted at rest (launch audit item 5, 2026-10-26): AES-256-GCM with a key used only for chats
# (data/.chat_key, 0600, never leaves the box). The file's own name is bound in (associated data), so one wallet's file copied
# over another's won't open. Files written before this are plain JSON: still read, and encrypted on their next save.
CHAT_MAGIC = b'KVC1'


def _chat_key():
    p = DATA / '.chat_key'
    if not p.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_bytes(secrets.token_bytes(32)); os.chmod(tmp, 0o600)
        try:
            os.link(tmp, p)                            # first writer wins; never replace a key that already encrypted files
        except FileExistsError:
            pass
        tmp.unlink(missing_ok=True)
    return p.read_bytes()


def _seal(f, data):
    nonce = secrets.token_bytes(12)
    return CHAT_MAGIC + nonce + AESGCM(_chat_key()).encrypt(nonce, data, f.name.encode())


def _open(f, blob):
    if not blob.startswith(CHAT_MAGIC):
        return blob                                    # written before encryption: plain JSON
    return AESGCM(_chat_key()).decrypt(blob[4:16], blob[16:], f.name.encode())


def list_chats(email):
    try:
        f = _file(email)
        return json.loads(_open(f, f.read_bytes()))
    except Exception:
        return []


def _write(email, chats):
    CHATS.mkdir(parents=True, exist_ok=True)
    f = _file(email); tmp = f.with_suffix('.tmp')
    tmp.write_bytes(_seal(f, json.dumps(chats[:MAX_CHATS]).encode())); os.chmod(tmp, 0o600); tmp.replace(f)


def _clean(chat):
    cid = str(chat.get('id') or '')
    if not ID_RE.match(cid):
        raise ValueError('bad id')
    turns = []
    for t in (chat.get('turns') or [])[-MAX_TURNS:]:
        turns.append({'u': str(t.get('u') or '')[:4000], 't': str(t.get('t') or '')[:8000],
                      'ts': int(t.get('ts') or 0) if str(t.get('ts') or '0').isdigit() else 0,
                      'p': t.get('p') if isinstance(t.get('p'), dict) else None})
    out = {'id': cid, 'title': str(chat.get('title') or '')[:80], 'turns': turns,
           'ts': int(chat.get('ts') or time.time() * 1000)}
    if len(json.dumps(out)) > MAX_CHAT_BYTES:
        raise ValueError('chat too large')
    return out


_ROOM = {'t': 0, 'ok': True}


def _room_for_new():
    """All saved chats together stay under a budget (audit V13): a NEW wallet's first save is refused when it's full;
    existing histories keep saving. Measured at most once a minute."""
    if time.time() - _ROOM['t'] > 60:
        try:
            files = list(CHATS.glob('*.json')) if CHATS.exists() else []
            _ROOM['ok'] = len(files) < 20000 and sum(f.stat().st_size for f in files) < 1_000_000_000
        except Exception:
            _ROOM['ok'] = True
        _ROOM['t'] = time.time()
    return _ROOM['ok']


def save_chat(email, chat, index=None):
    c = _clean(chat)
    if not _file(email).exists() and not _room_for_new():
        raise ValueError('chat storage is full right now')
    with _lock:
        chats = [x for x in list_chats(email) if x.get('id') != c['id']]
        chats.insert(max(0, min(len(chats), index)) if isinstance(index, int) else 0, c)
        while len(chats) > 1 and len(json.dumps(chats)) > 2_000_000:   # one account keeps at most ~2 MB: the oldest chats go first (review F-10)
            chats.pop()
        _write(email, chats)


def delete_chat(email, cid):
    with _lock:
        _write(email, [x for x in list_chats(email) if x.get('id') != str(cid)])


# ── wallet sign-in ──
ADDR = re.compile(r'^0x[0-9a-f]{64}$')
GRAPHQL = 'https://graphql.mainnet.sui.io/graphql'
_pending = {}            # nonce -> (address, message, expires)

# Terms of Service (2026-10-24): the sign-in message doubles as the agreement, so the user signs once. Switched on by
# the file data/terms_on (after the owner approves the text); each signed agreement is kept in data/customers.db with the
# wallet address, time, exact message and signature, and saved to the wallet's Walrus space by projects/kivy/walrus.
TERMS_VERSION = 'v1'
TERMS_URL = 'https://kivy.kivelabs.xyz/terms'
CUSTOMERS = DATA / 'customers.db'


def terms_on():
    return (DATA / 'terms_on').exists()


def _cdb():
    c = sqlite3.connect(CUSTOMERS, timeout=10)
    c.execute('CREATE TABLE IF NOT EXISTS customers (address TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, sign_ins INTEGER)')
    c.execute('CREATE TABLE IF NOT EXISTS agreements (id INTEGER PRIMARY KEY, address TEXT, version TEXT, ts REAL, message TEXT, signature TEXT)')
    c.execute('CREATE INDEX IF NOT EXISTS agreements_addr ON agreements(address)')
    return c


def record_sign_in(address, message, signature):
    """A verified sign-in: the customer row, and the agreement when the message carried the Terms."""
    now = time.time()
    with _cdb() as c:
        c.execute('INSERT INTO customers VALUES(?,?,?,1) ON CONFLICT(address) DO UPDATE SET last_seen = excluded.last_seen, '
                  'sign_ins = sign_ins + 1', (address, now, now))
        v = re.search(r"Kivy's Terms of Service \((v\d+)\)", message or '')
        if v:
            c.execute('INSERT INTO agreements(address, version, ts, message, signature) VALUES(?,?,?,?,?)',
                      (address, v.group(1), now, message, signature))


def agreed(address):
    """When the wallet agreed to the current Terms (unix time), or None."""
    address = str(address or '').lower()
    if not ADDR.match(address) or not CUSTOMERS.exists():
        return None
    with _cdb() as c:
        r = c.execute('SELECT max(ts) FROM agreements WHERE address = ? AND version = ?', (address, TERMS_VERSION)).fetchone()
    return r[0] if r and r[0] else None


def challenge(address):
    address = str(address or '').lower()
    if not ADDR.match(address):
        raise ValueError('bad address')
    now = time.time()
    for k in [k for k, v in _pending.items() if v[2] < now]:
        _pending.pop(k, None)
    mine = sorted((v[2], k) for k, v in _pending.items() if v[0] == address)
    for _, k in mine[:max(0, len(mine) - 4)]:          # at most 5 open sign-ins per address: the newest win (audit V12)
        _pending.pop(k, None)
    if len(_pending) >= 5000:                           # a burst of many addresses: refuse until some expire
        raise ValueError('busy, try again in a minute')
    nonce = secrets.token_hex(12)
    if terms_on():
        msg = ("Sign in to Kivy\n\nI agree to Kivy's Terms of Service (%s) and Privacy Policy: %s\n\n"
               'This proves you own this wallet. It is not a transaction and costs nothing.\n\nAddress: %s\nNonce: %s\nIssued: %s UTC'
               % (TERMS_VERSION, TERMS_URL, address, nonce, time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))))
    else:
        msg = ('Sign in to Kivy\n\nThis only proves you own this wallet so your chats stay private. '
               'It is not a transaction and costs nothing.\n\nAddress: %s\nNonce: %s\nIssued: %s UTC'
               % (address, nonce, time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))))
    _pending[nonce] = (address, msg, now + 600)
    return msg


def _uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7f; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _personal_digest(msg_bytes):
    return hashlib.blake2b(bytes([3, 0, 0]) + _uleb(len(msg_bytes)) + msg_bytes, digest_size=32).digest()


def verify_personal(address, msg_bytes, sig_b64):
    """True when sig_b64 is `address`'s Sui signature over the personal message."""
    raw = base64.b64decode(sig_b64)
    flag = raw[0]
    if flag in (0, 1, 2):
        sig, pk = raw[1:65], raw[65:]
        if '0x' + hashlib.blake2b(bytes([flag]) + pk, digest_size=32).hexdigest() != address:
            return False
        digest = _personal_digest(msg_bytes)
        try:
            if flag == 0:
                ed25519.Ed25519PublicKey.from_public_bytes(pk).verify(sig, digest)
            else:
                curve = ec.SECP256K1() if flag == 1 else ec.SECP256R1()
                key = ec.EllipticCurvePublicKey.from_encoded_point(curve, pk)
                der = ec_utils.encode_dss_signature(int.from_bytes(sig[:32], 'big'), int.from_bytes(sig[32:], 'big'))
                key.verify(der, digest, ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:
            return False
    # zkLogin (e.g. Slush with Google), passkey, multisig: ask the Sui network to check it
    q = ('query($m: Base64!, $s: Base64!, $a: SuiAddress!) { verifySignature(message: $m, signature: $s, '
         'intentScope: PERSONAL_MESSAGE, author: $a) { success } }')
    body = json.dumps({'query': q, 'variables': {'m': base64.b64encode(msg_bytes).decode(), 's': sig_b64, 'a': address}}).encode()
    req = urllib.request.Request(GRAPHQL, data=body, headers={'Content-Type': 'application/json', 'User-Agent': 'kivy/1'})
    out = json.load(urllib.request.urlopen(req, timeout=20))
    return bool(((out.get('data') or {}).get('verifySignature') or {}).get('success'))


def sign_in_wallet(address, message, sig_b64):
    """Return the address when the one-time message was signed by it, else raise ValueError."""
    address = str(address or '').lower()
    m = re.search(r'Nonce: ([0-9a-f]{24})', str(message or ''))
    hit = _pending.pop(m.group(1), None) if m else None
    if not hit or hit[0] != address or hit[1] != message or hit[2] < time.time():
        raise ValueError('sign-in expired, try again')
    if not verify_personal(address, message.encode(), str(sig_b64 or '')):
        raise ValueError('signature does not match this wallet')
    return address
