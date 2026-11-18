"""Field caps at the door (fix package 2 "one person can't freeze Kivy", a reviewer scan 2026-11-15): every string in a JSON POST body
is cut to a set length before any route reads it, so one 24 kB field can't feed a slow pattern (S1: metrics.redact on /api/m and
/api/feedback held the stats lock ~2 s; R5: brain's history redaction took 5.6 s on one 23 kB turn). The lengths sit well above
what the page sends (chat message 500, history 20 turns of 1,200, feedback note 1,000, event detail 60) and above every valid
value's length, so a cut can never turn a refused value into an accepted one (an over-long address stays over-long).
"""
# How it runs: imported by site/server.py; Handler._post calls cap_body(path, raw) once, right after the whole body arrived and
# before a slot is taken. A body that is not JSON, or that fits every cap, comes back unchanged (the same string); only a body
# with a string over its cap is parsed, cut and written again. Tested by tests/test_stability_20261115.py.
import json

TEXT = 4000                  # any string the table below does not name, at any depth
ROUTES = {                   # route -> {top-level field: cap}; 'history' = the chat's turns: the last 20, each field capped
    '/api/chat': {'message': 2000, 'address': 128, 'vid': 64,
                  'history': {'last': 20, 'text': 1200, 'role': 16, 'content': 1200}},
    '/api/m': {'d': 200, 'ev': 40, 'vid': 64, 'address': 128},
    '/api/feedback': {'note': 2000, 'ask': 1000, 'answer': 1000, 'vid': 64},
    '/api/auth/wallet': {'signature': 20000},       # zkLogin and passkey signatures are long; the body limit (24 kB) holds them
}
SKIP = {'/api/chats/save'}   # a saved chat is stored as the user wrote it: accounts.save_chat caps it, the 160 kB body limit holds it


def _cut(v, n, hit):
    if isinstance(v, str) and len(v) > n:
        hit.append(1)
        return v[:n]
    return v


def _turns(turns, rule, hit):
    """The chat's history: the last `rule['last']` turns, each named field capped. A turn's text that is not a string (a
    nested object) becomes its text form, capped — the readers call str() on it anyway."""
    if len(turns) > rule['last']:
        hit.append(1)
        turns = turns[-rule['last']:]
    for h in turns:
        if isinstance(h, dict):
            for f, n in rule.items():
                if f != 'last' and f in h:
                    if isinstance(h[f], (dict, list)):
                        h[f] = str(h[f])
                        hit.append(1)
                    h[f] = _cut(h[f], n, hit)
    return turns


def cap_body(path, raw):
    """The request body with every string field at most its cap: `raw` itself when nothing was over (or it is not JSON)."""
    if path in SKIP or not raw or len(raw) <= 16:
        return raw
    try:
        body = json.loads(raw)
    except Exception:
        return raw                                   # not JSON: the route answers it as before
    rules, hit, stack = ROUTES.get(path, {}), [], []
    if isinstance(body, dict):
        for k in list(body):
            r = rules.get(k)
            if isinstance(r, int) and isinstance(body[k], str):
                body[k] = _cut(body[k], r, hit)
            elif isinstance(r, dict) and isinstance(body[k], list):
                body[k] = _turns(body[k], r, hit)
                stack.append(body[k])
            elif isinstance(body[k], str):
                body[k] = _cut(body[k], TEXT, hit)
            elif isinstance(body[k], (dict, list)):
                stack.append(body[k])
    elif isinstance(body, list):
        stack.append(body)
    elif isinstance(body, str):
        body = _cut(body, TEXT, hit)
    while stack:                                     # every nested string, at any depth, without recursion
        x = stack.pop()
        for k in (range(len(x)) if isinstance(x, list) else list(x)):
            v = x[k]
            if isinstance(v, str):
                if len(v) > TEXT:
                    x[k] = _cut(v, TEXT, hit)
            elif isinstance(v, (dict, list)):
                stack.append(v)
    return json.dumps(body, ensure_ascii=False) if hit else raw
