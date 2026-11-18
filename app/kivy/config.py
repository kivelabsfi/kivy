"""Config helpers: coin display names, the $ cap, K() = knowledge/kivy.json hot-reloaded (Section 12).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# K() reads server.py's KNOWLEDGE path, handed in by server.py right after this module loads.
import json
import re

KNOWLEDGE = None           # server.py's (Section 1): set by server.py once it is defined (a module never imports server.py)


# ── Section 12: config helpers — coin display names, the $ cap and K() (knowledge/kivy.json, hot-reloaded);
#    called from nearly every reader, card and route ──
SPOT_NAME = {'BTC': 'xBTC'}                   # what the spot coin is really called on Sui
SPOT_IN = {'XBTC': 'BTC'}                     # typed / AI names -> Kivy's internal key


# Kivy key -> the name users see ('BTC' -> 'xBTC' on spot).
def disp(sym):
    return SPOT_NAME.get(sym, sym)


def disp_text(t):
    """Spot texts only: say xBTC, never plain BTC."""
    return re.sub(r'(?<![A-Za-z])BTC(?![A-Za-z])', 'xBTC', t) if isinstance(t, str) else t


# A coin type NOT on Kivy's coin list (QA 2026-11-17 OPS-V1 / ATK-V3, fix package 4 "No fake coins"): anyone can publish a coin
# whose type ends ::usdc::USDC (or ::sui::SUI, or any listed name) and send it to a user. Such a coin never gets a listed coin's
# name, icon or dollar value: it is "unknown coin" + its shortened type. Module and struct names are kept only when they look
# like names, so a coin named as an instruction never reaches the AI (review W2-14). The page's unkCoin() applies the same rule.
_COIN_PART = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,11}')


def unknown_coin(t):
    """'0x00…deadbeef::usdc::USDC' -> 'unknown coin 0xdead…::usdc::USDC' ('0x' optional); no readable type -> 'unknown coin'."""
    m = re.match(r'(?:0x)?0*([0-9a-fA-F]{1,64})::([A-Za-z0-9_]+)::([A-Za-z0-9_]+)', str(t or '').strip())
    if not m:
        return 'unknown coin'
    pkg, mod, name = m.groups()
    names = '::%s::%s' % (mod, name) if _COIN_PART.fullmatch(mod) and _COIN_PART.fullmatch(name) else ''
    return 'unknown coin 0x%s%s%s' % (pkg[:4].lower(), '…' if len(pkg) > 4 else '', names)


def cap_usd():
    """Kivy's $ limit per swap / send / lend, or None when off (knowledge/kivy.json cap_usd: 0). 2026-10-23: no limit —
    the user approves every transaction in their own wallet."""
    try:
        v = float(K().get('cap_usd') or 0)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def ui_cap():
    """The cap the cards use for sliders and Max: unlimited when there is none."""
    return cap_usd() or 1e12


_KC = {'m': None, 'v': None}
_PAGE = {}                                           # the built index page + its ETag, keyed on the files' mtimes


def K():
    """Re-read only when the file changes, so editing it still changes Kivy without a restart (it was parsed ~100 times a
    request — review W-06). A shallow copy each call: callers may add keys to it."""
    m = KNOWLEDGE.stat().st_mtime_ns
    if _KC['m'] != m:
        _KC['v'], _KC['m'] = json.loads(KNOWLEDGE.read_text()), m
    return dict(_KC['v'])
