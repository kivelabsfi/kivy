"""Kivy's AI layer — PUBLIC, TRIMMED copy of brain.py.

Tool CALLING, never tool RUNNING. The model reads the chat and may REQUEST one of the 10 tools below. Kivy's own code
(passed in as `tools`) decides whether and how to run it, under the same rules as every other request. Every tool only
reads data or produces a PLAN; none can sign, send or execute anything: a plan becomes a card, the user approves it,
check 1 (app/guard.js) re-simulates it before the wallet opens, and check 2 (app/server.py) compares the landed
transaction with the plan. Every reply passes the leak filter. If the AI is unavailable, the caller falls back to
Kivy's rule-based replies, so cards and transactions keep working without it.

What is PRIVATE and left out: the system prompt (Kivy's persona and instructions), the model list and fallback tuning,
the provider calls and API keys, and reply polishing. Those parts are stubs here and raise if called.
Copied word for word from the live module: TOOLS, _leak_re / LEAK, _blocked / BLOCKED.
"""
import json
import os
import re

MAX_ROUNDS = 3   # at most 3 model turns per message (request a tool -> get its result -> answer)

# ── the only things the model can ask for (live definitions; Kivy offers a subset per message) ──
TOOLS = [{'functionDeclarations': [
    {'name': 'get_balances', 'description': "The user's connected wallet balances (read-only).",
     'parameters': {'type': 'OBJECT', 'properties': {}}},
    {'name': 'get_prices', 'description': 'Live USD prices for supported coins.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'assets': {'type': 'ARRAY', 'items': {'type': 'STRING'},
                    'description': 'Coin symbols, e.g. ["SUI","DEEP"]. Empty for all.'}}}},
    {'name': 'plan_swap', 'description': 'Build a swap PLAN (nothing is executed).',
     'parameters': {'type': 'OBJECT', 'properties': {
         'amount': {'type': 'STRING', 'description': 'a number, or all / max / half of what the wallet holds'}, 'from_asset': {'type': 'STRING'}, 'to_asset': {'type': 'STRING'}},
         'required': ['amount', 'from_asset', 'to_asset']}},
    {'name': 'plan_lend', 'description': 'Lend (supply) any coin Suilend or NAVI lists — USDC, SUI, BTC, WAL, DEEP, ETH, SOL, and versions like vSUI, sSUI, LBTC, USDsui. USDC gets a plan; other coins open a lending card, and if the user has none of that coin the card swaps another coin into it and lends it in ONE transaction. The user signs it.',
     'parameters': {'type': 'OBJECT', 'properties': {'amount': {'type': 'STRING', 'description': 'a number, or all / max / half of what the wallet holds'}, 'asset': {'type': 'STRING'},
                    'protocol': {'type': 'STRING', 'description': 'suilend or navi; leave empty for the higher rate'}},
                    'required': ['amount', 'asset']}},
    {'name': 'plan_send', 'description': 'Build a PLAN to send a coin to another address or .sui name (nothing is executed; the user approves in their wallet).',
     'parameters': {'type': 'OBJECT', 'properties': {'amount': {'type': 'STRING', 'description': 'a number, or all / max / half of what the wallet holds'}, 'asset': {'type': 'STRING'},
                    'recipient': {'type': 'STRING', 'description': 'Full 0x address or a .sui name'}},
                    'required': ['amount', 'asset', 'recipient']}},
    {'name': 'ask_choice', 'description': 'When the request is unclear or could mean several jobs: offer 2-4 tappable options (each one is a job from TOOLS.md) and ask the user to confirm one. Each option has a short label and the exact message it sends when tapped.',
     'parameters': {'type': 'OBJECT', 'properties': {'question': {'type': 'STRING', 'description': 'one short question, e.g. Did you mean…?'},
                    'options': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
                        'label': {'type': 'STRING', 'description': 'what the user sees, e.g. Long or short BTC (perps)'},
                        'message': {'type': 'STRING', 'description': 'the exact chat message sent on tap, e.g. chart BTC, swap USDC to BTC, wallet balance, market, lend, perps, positions'}},
                        'required': ['label', 'message']}}}, 'required': ['question', 'options']}},
    {'name': 'show_card', 'description': 'Show a live card under your reply. Use it whenever the user talks about a market, a coin to trade, prices, perps, their positions or lending: chart (live chart + trade panel for one market, needs symbol), markets (all perps markets table), perps (their perps account), positions (their positions/orders/history), lending (lending markets table). At most one per reply.',
     'parameters': {'type': 'OBJECT', 'properties': {'card': {'type': 'STRING', 'description': 'trade (filled-in order panel) | chart | markets | perps | perps_deposit | perps_withdraw | positions | lending | lending_withdraw | rewards'},
                    'side': {'type': 'STRING', 'description': 'trade: long or short'},
                    'usd': {'type': 'STRING', 'description': 'trade: order size in USD, if the user said a size'},
                    'margin': {'type': 'STRING', 'description': "trade: USDC the user puts in, or 'max' for all their free USDC (Kivy sizes the biggest order it allows)"},
                    'order_type': {'type': 'STRING', 'description': 'trade: market or limit'},
                    'price': {'type': 'STRING', 'description': 'trade: limit price, if a limit order'},
                    'leverage': {'type': 'STRING', 'description': "trade: a number or 'max' (market's maximum)"},
                    'symbol': {'type': 'STRING', 'description': 'market symbol for chart, e.g. BTC, ETH, SOL, GOLD'}}, 'required': ['card']}},
    {'name': 'get_transaction', 'description': 'Read a finished transaction from the Sui chain: what was sent, from whom, to whom, the fee and the time. Leave digest empty to use the last transaction in this chat.',
     'parameters': {'type': 'OBJECT', 'properties': {'digest': {'type': 'STRING', 'description': 'transaction id (digest); optional'}}}},
    {'name': 'plan_withdraw', 'description': 'Build a PLAN to withdraw lent USDC back to the wallet; the user signs it.',
     'parameters': {'type': 'OBJECT', 'properties': {'amount': {'type': 'STRING', 'description': 'a number, or all'}, 'asset': {'type': 'STRING'},
                    'protocol': {'type': 'STRING', 'description': 'suilend or navi; leave empty for where the user has funds'}},
                    'required': ['amount', 'asset']}},
    {'name': 'get_lending', 'description': 'Live lending rates on Suilend and NAVI for ANY coin (USDC, USDsui, SUI, sSUI, vSUI, BTC, WAL…), the collateral each market accepts (its risk), and every coin the user has lent and where. Pass asset to get that coin\'s markets; leave it empty for USDC.',
     'parameters': {'type': 'OBJECT', 'properties': {'asset': {'type': 'STRING', 'description': 'The coin, e.g. USDsui, SUI, sSUI. Empty = USDC.'}}}},
]}]


# Anything that would reveal the rest of this server. A reply containing one is not shown.
def _leak_re():
    """The private words (names of other projects and people on this server) live in a file outside the code, so
    publishing the code doesn't publish them. Without the file, the generic patterns still apply."""
    words = []
    try:
        words = [w.strip() for w in open(os.environ.get('KIVY_LEAK_FILE', '/opt/kivy/data/leak_words.txt')) if w.strip() and not w.startswith('#')]
    except OSError:
        pass
    return re.compile(r'\b(' + '|'.join(words + [r'/root', r'/opt', r'secrets?/']) + r')\b|suiprivkey', re.I)


LEAK = _leak_re()


def _blocked(text, plan, message=''):
    # An address the user typed themselves is not a leak: blank those out before checking
    # (e.g. "send 1 USDC to 0x4cc5…" must still work; Kivy must just never volunteer it).
    mine = set(a.lower() for a in re.findall(r'0x[0-9a-fA-F]{6,64}', message or ''))
    def clean(x):
        return re.sub(r'0x[0-9a-fA-F]{6,64}', lambda m: '0x' if m.group(0).lower() in mine else m.group(0), x)
    return LEAK.search(clean(text)) or (plan and LEAK.search(clean(json.dumps(plan))))


BLOCKED = {'text': "I can only talk about my own features — swaps, lending, prices and how I work.",
           'leak_blocked': True}


# ── private parts (stubs) ──
def system_prompt(k: dict, wallet_connected: bool = False) -> str:
    """PRIVATE: Kivy's persona and instructions, built from private files plus knowledge/kivy.json."""
    raise NotImplementedError('private')


def _ask_model(conversation: list, tool_defs: list) -> dict:
    """PRIVATE: model choice, fallbacks, provider call and API key. Returns {'text': str} or {'tool_calls': [...]},
    each call {'id', 'name', 'args'}."""
    raise NotImplementedError('private')


def humanize(text: str) -> str:
    """PRIVATE: reply wording polish."""
    return text


# ── the loop: the same steps as the live loop, provider details removed ──
def chat(message: str, history: list, k: dict, tools: dict, wallet_connected: bool = False, allow: str = '') -> dict | None:
    """Returns {'text', 'plan'?} or None when the AI is unavailable (the caller falls back to rule-based replies).
    tools: name -> callable(args) returning a dict. These are KIVY's functions; the model only names one."""
    offered = [f for f in TOOLS[0]['functionDeclarations'] if f['name'] in tools]   # only the tools Kivy offers this time
    plan = None
    try:
        conv = [{'role': 'system', 'content': system_prompt(k, wallet_connected)}]
        for h in (history or [])[-20:]:
            t = str(h.get('text') or '')[:600]
            if t:
                conv.append({'role': 'assistant' if h.get('role') == 'model' else 'user', 'content': t})
        conv.append({'role': 'user', 'content': message[:500]})
        for _ in range(MAX_ROUNDS):
            r = _ask_model(conv, offered)
            calls = r.get('tool_calls') or []
            if not calls:                                         # a final answer: screen it, then show it
                text = (r.get('text') or '').strip()
                if not text:
                    return None
                if _blocked(text, plan, allow + ' ' + message + ' ' + ' '.join(str((h or {}).get('text') or '') for h in (history or []))):
                    return dict(BLOCKED)
                text = humanize(text)
                return {'text': text, 'plan': plan} if plan else {'text': text}
            conv.append({'role': 'assistant', 'tool_calls': calls})
            for c in calls:                                       # Kivy runs the tool, never the model
                fn = tools.get(c.get('name'))
                try:                                              # a broken tool must not take the whole reply down
                    out = fn(c.get('args') if isinstance(c.get('args'), dict) else {}) if fn else {'error': 'unknown tool'}
                except Exception:
                    out = {'error': 'that lookup failed just now; answer without it or say it is unavailable'}
                if isinstance(out, dict) and (out.get('plan') or out.get('_plan')):
                    plan = out.get('plan') or out['_plan']        # the card shown is Kivy's, not the model's
                if isinstance(out, dict):                         # '_' keys are for Kivy only, never sent to the model (privacy)
                    out = {key: v for key, v in out.items() if not str(key).startswith('_')}
                conv.append({'role': 'tool', 'tool_call_id': c.get('id'), 'content': json.dumps(out)})
    except NotImplementedError:
        return None
    return None
