"""Intent: which job, coin and market a message means; asks with tappable choices (Section 9).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# intent_ask is called by code_answer and widget_reply; wallet_snapshot feeds the AI prompt (server.py).
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import difflib
import json
import re
import unicodedata
from pathlib import Path
from . import amounts as AM
from .config import disp
from .intentwords import AMOUNT_WORD_RE, COIN_NAMES, INTENT_FORMS, INTENT_STOP, QUESTION_RE, VERB_OF
from .patterns import ADDR_NAME_RE
from .upstream import lend_cli

widget_reply = None        # server.py's (Section 7): set by server.py once it is defined (a module never imports server.py)


def not_english(msg):
    """True when the message has letters from another script (Thai, Chinese, Cyrillic…). Kivy reads English only
    (2026-10-20) and says so instead of guessing. Emoji, numbers and symbols are fine."""
    for ch in msg:
        if ch.isalpha() and ord(ch) > 0x24F:
            try:
                if 'LATIN' not in unicodedata.name(ch):
                    return True
            except ValueError:
                return True
    # Latin-script languages too ("quanto ho nel portafoglio?" got a typo suggestion, 2026-10-23): most real words not English
    try:
        bare = ADDR_NAME_RE.sub(' ', msg.lower())   # addresses and names aren't language
        words = [w for w in re.findall(r"[a-z]{3,}", bare) if not ckey(w) and not chart_symbol(w)]
        known = ENGLISH | {'kivy', 'usdc', 'navi', 'suilend', 'perps', 'perp', 'lend', 'lending', 'swap', 'apy', 'apr', 'defi', 'crypto',
                           'pls', 'plz', 'thx', 'ape', 'lol', 'hey', 'gm', 'yo', 'wanna', 'gonna', 'ok', 'okay',
                           'ember', 'matrixdock', 'aftermath', 'vault', 'vaults', 'collateral', 'usdsui', 'perpetual', 'perpetuals'}
        foreign = [w for w in words if w not in known and not near_english(w)]
        if len(foreign) >= 3 and len(foreign) * 10 >= len(words) * 6:
            return True
        # short requests ("cuanto sui tengo", "envoyer 1 usdc") have too few words for the share rule: everyday words of other
        # Latin-script languages decide — two of them, or one when no English word stands beside it (2026-11-13)
        hits = [w for w in re.findall(r"[a-zà-ÿ]{2,}", bare) if w in FOREIGN_WORDS]
        return len(hits) >= 2 or (len(hits) == 1 and all(w in FOREIGN_WORDS for w in words))
    except Exception:
        return False


# Everyday words of Spanish, French, Portuguese, Italian, German, Indonesian/Malay, Vietnamese (ASCII forms), Turkish: none is an
# English word, a coin or a market (checked at import against the word list). Used by not_english() for short sentences.
FOREIGN_WORDS = set('cuanto cuanta cuantos tengo tienes quiero quieres enviar envia mandar cambiar comprar vender prestar retirar depositar dinero monedas '
                    'billetera cartera cuenta saldo precio hola gracias por favor ayuda puedo puedes necesito donde cuando porque '
                    'envoyer acheter vendre preter retirer deposer echanger combien argent portefeuille solde prix bonjour merci veux voudrais peux pouvez '
                    'quanto quanta tenho quero enviar trocar emprestar sacar depositar dinheiro carteira saldo preco obrigado obrigada posso preciso '
                    'voglio vorrei inviare comprare vendere prestare prelevare scambiare soldi portafoglio saldo prezzo ciao grazie posso quanti '
                    'wieviel wie viel ich habe möchte moechte senden kaufen verkaufen tauschen leihen abheben einzahlen geld guthaben preis hallo danke bitte kann '
                    'berapa saya punya mau ingin kirim tukar beli jual pinjam tarik setor uang dompet saldo harga terima kasih tolong bisa '
                    'bao nhieu toi muon gui doi mua ban tien vi cam on '
                    'kadar ne istiyorum gonder satin almak satmak para cuzdan bakiye fiyat merhaba tesekkur'.split())


def other_unknown(msg):
    """Words Kivy neither knows nor can read as a typo — then a "did you mean" would be a guess at a foreign sentence
    ("quanto ho nel portafoglio?" -> "portfolio?", 2026-10-23): let the AI answer instead (it replies in English)."""
    try:
        fixed = {f[0].lower() for f in (intent_parse(msg).get('fixes') or [])}
        bare = ADDR_NAME_RE.sub(' ', msg.lower())
        wallets = ['slush', 'vera', 'nightly', 'phantom', 'suiet', 'surf', 'okx', 'bitget', 'martian', 'ethos', 'backpack', 'wallet']   # wallet names, typos too ("slsuh")
        return [w for w in re.findall(r'[a-z]{3,}', bare) if w not in fixed and w not in ENGLISH and not ckey(w) and not chart_symbol(w)
                and not near_english(w) and w not in {'kivy', 'navi', 'suilend', 'perps', 'ember', 'matrixdock', 'aftermath', 'usdsui', 'pls', 'plz', 'ape', 'apy', 'apr'}
                and not difflib.get_close_matches(w, wallets, n=1, cutoff=0.6)]
    except Exception:
        return []


def near_english(w, cutoff=0.8):
    """A word one typo away from a common English word ('swpa', 'balanse') — still English."""
    if len(w) < 4:
        return False
    return bool(difflib.get_close_matches(w, ENGLISH_LONG.get(w[0], ()), n=1, cutoff=cutoff))


try:                                                  # 10,000 common English words (google-10000-english, no swears): never "fixed" as typos
    ENGLISH = set(Path(__file__).resolve().parent.parent.joinpath('words_en.txt').read_text().split())
    ENGLISH_LONG = {}
    for _w in ENGLISH:
        if len(_w) >= 4:
            ENGLISH_LONG.setdefault(_w[0], []).append(_w)
except Exception:
    ENGLISH = set()
    ENGLISH_LONG = {}
FOREIGN_WORDS = {w for w in FOREIGN_WORDS if len(w) >= 4 and w not in ENGLISH} if ENGLISH else set()   # no word list: the short-sentence rule is off
NEVER_FIX = {'paused', 'pause', 'pauses', 'pausing', 'unlent', 'loose', 'unchanged', 'retrieve', 'chats', 'chat', 'wallets', 'accounts', 'usdt', 'twap', 'tell', 'their', 'year', 'great', 'buggy', 'fetch', 'fetcher', 'headings', 'learns', 'shortcut', 'pepe', 'rebalance', 'rebalancing', 'rebalanced', 'equalize', 'equalise', 'consolidate', 'staked', 'staking', 'unstake', 'tidy', 'leftovers', 'dust', 'safest', 'collateral', 'everywhere', 'gas'}   # + the new-situation words, 2026-10-24 ('rebalance' became 'balance')   # USDT is another coin, TWAP an order type (2026-10-22 scan)


LEND_MARKET_WORDS = ('navi', 'suilend', 'ember', 'matrixdock')   # lending-market words a typo after on/in/from is read as (review P-25)


def stem_tool(lw):
    """'balances' -> balance, 'deposits' -> deposit, 'shorting' -> short, 'withdrawal' -> withdraw: an ending, not a typo."""
    for end in ('ing', 'es', 'ed', 'al', 's'):
        if lw.endswith(end) and len(lw) - len(end) >= 3:
            base = lw[:-len(end)]
            for b in (base, base + 'e'):
                if b in VERB_OF:
                    return VERB_OF[b]
    return None


def intent_parse(msg):
    """{'tools', 'coins', 'fixes'} — the jobs and coins a message names, typos corrected (fixes = [(typed, meant)])."""
    tools, coins, fixes = [], [], []
    # a word after on/in/from in a lending sentence is a market first: "lend 5 usdc on nave" is NAVI, not the coin NAVX (review P-25)
    at_mkt = {m.group(1).lower() for m in re.finditer(r'\b(?:on|in|into|at|from)\s+(?:the\s+|my\s+)?([A-Za-z]{4,})\b', msg, re.I)} \
        if re.search(r'\b(?:lend\w*|deposit\w*|supply|withdr[ae]w\w*|redeem|unlend|park|put|move)\b', msg, re.I) else set()
    for w in re.findall(r'[A-Za-z][A-Za-z0-9.]*', msg):
        w = w.rstrip('.')                            # "…to vSUI." is not a typo (2026-10-21)
        lw = w.lower()
        if lw in COIN_NAMES:
            coins.append(COIN_NAMES[lw]); continue
        if lw in VERB_OF:
            tools.append(VERB_OF[lw]); continue
        if lw in INTENT_STOP or lw in INTENT_FORMS or market_only(w):
            continue
        k = ckey(w) if (len(lw) >= 3 or w.isupper()) else None
        if k:
            coins.append(k); continue
        st = stem_tool(lw)
        if st:                                       # an ending of a job word: understood, no "did you mean"
            tools.append(st); continue
        if lw in at_mkt and lw not in NEVER_FIX:
            mk = [x for x in difflib.get_close_matches(lw, list(LEND_MARKET_WORDS), n=1, cutoff=0.74) if lw not in ENGLISH or len(x) == len(lw)]
            if mk:                                   # 'on nave' / 'from sulend': the market (review P-25); a real word only if one letter is off ('navy', not 'member')
                fixes.append((w, mk[0])); continue
        if lw in ENGLISH or lw in NEVER_FIX or chart_symbol(w):   # a real word or a real market is not a typo (tell -> sell, chat -> chart…)
            continue
        if (lw[:2] in ('un', 're') and (lw[2:] in ENGLISH or lw[2:] in VERB_OF)) or re.match(r'^(?:four|fif|six|seven|eigh|nin|ten|eleven|twelf|twentie|thirtie|hundred|thousand)ths?$', lw) \
                or lw in ('third', 'thirds', 'quarter', 'quarters', 'halves', 'halve', 'twice', 'thrice'):
            continue                                 # "unchanged" is not "change", "tenth" is not ETH (2026-10-31), "thirds" is not eTHIRD (2026-11-07)
        if len(lw) >= 4:                             # a typo of a job or a coin?
            v = difflib.get_close_matches(lw, list(VERB_OF), n=1, cutoff=0.74)   # 'swpa' -> swap
            if v:
                tools.append(VERB_OF[v[0]]); fixes.append((w, v[0])); continue
            nm = difflib.get_close_matches(lw, list(COIN_NAMES), n=1, cutoff=0.8)       # 'etherium' -> ETH
            if nm:
                coins.append(COIN_NAMES[nm[0]]); fixes.append((w, COIN_NAMES[nm[0]])); continue
            names = {x.lower(): x for x in ecoins()}
            c = difflib.get_close_matches(lw, list(names), n=1, cutoff=0.75)            # 'vsiu' -> vSUI, 'wbct' -> wBTC
            if c:
                coins.append(names[c[0]]); fixes.append((w, names[c[0]])); continue
            vn = difflib.get_close_matches(lw, ['aftermath', 'suilend', 'navi', 'perps', 'margin', 'matrixdock', 'ember', 'collateral'], n=1, cutoff=0.78)
            if vn:                                   # 'aftremath' -> aftermath, 'perpz' -> perps
                fixes.append((w, vn[0]))
    if re.search(r'\b(?:take|pull|get|cash|move)\b(?:\s+\w+){0,3}\s+out\b|\b(?:take|get|bring|want)\s+(?:\w+\s+){0,2}back\b', msg, re.I):
        tools.insert(0, 'withdraw')                  # "take my USDsui out", "pull it out", "take it back"
    dedup = lambda xs: [x for i, x in enumerate(xs) if x not in xs[:i]]
    return {'tools': dedup(tools), 'coins': dedup(coins), 'fixes': fixes}


def intent_option(tool, c, address):
    """One tappable choice: (label, sub, message), or None when it doesn't apply to this coin / wallet."""
    hold = (all_balances(address) or {}) if address else {}
    held = float(hold.get(c) or 0) > 0
    if tool == 'lend':
        rows = lend_rows_for(c) if c else []
        if c and not rows:
            return None
        return ('Lend %s' % disp(c) if c else 'Lend', ('Best now: %s · %.2f%% a year' % (LEND_MKTS.get(rows[0]['market'], rows[0]['name']), float(rows[0]['total_apr'])))
                if rows else 'See every lending market', 'lend %s' % c if c else 'lending markets')
    if tool == 'withdraw':
        mine = [r for r in other_lent(address) if r['coin'] == c] if (address and c and c != 'USDC') else []
        if c == 'USDC' and address:
            mine = [1 for p, v in (lend_positions(address) or {}).items() if float((v or {}).get('usdc') or 0) >= 0.01]
        if c and not mine:
            return None
        return ('Withdraw your %s' % disp(c) if c else 'Withdraw', 'From lending back to your wallet', 'withdraw %s' % c if c else 'withdraw')
    if tool == 'swap':
        if not c:
            return ('Swap', 'Trade one coin for another', 'swap')
        return (('Swap %s to another coin' % disp(c), 'You hold %s' % dec(float(hold.get(c) or 0)), 'swap %s to %s' % (c, 'USDC' if c != 'USDC' else 'SUI'))
                if held else ('Buy %s' % disp(c), 'Swap another coin into it', 'swap %s to %s' % ('USDC' if c != 'USDC' else 'SUI', c)))
    if tool == 'perps':
        sym = chart_symbol(c) if c else None
        return ('Long or short %s' % sym, 'Perps with leverage', 'open %s perps' % sym) if sym else (None if c else ('Perps', 'Long or short with leverage', 'perps'))
    if tool == 'price':
        sym = chart_symbol(c) if c else None
        return ('%s price & chart' % sym, 'Live chart', 'chart %s' % sym) if sym else None
    if tool == 'send':
        return ('Send %s' % disp(c), 'To an address or a .sui name', 'send %s' % c) if (c and held) else None
    if tool == 'claim':
        return ('Claim lending rewards', 'All markets in one go', 'claim rewards')
    if tool == 'balance':
        return ('Wallet balance', 'Every coin, lent and in perps', 'wallet balance')
    return None


def best_lend(msg, address, history=None, p=None):
    """"deposit all USDC in to suilend best yield market": the lend card on that best market, amount kept (2026-10-20)."""
    p = p or intent_parse(msg)
    bm = re.search(r'\b(lend|deposit|supply|put|park)\b(.*?)\b(?:in\s*to|into|on|in|at)\s+(?:the\s+)?(?:(suilend|navi)(?:\'?s)?\s+)?(?:best|highest|top)\b(?:\s+(?:yield|rate|apy|apr))?(?:\s+(suilend|navi))?', msg, re.I)
    if bm and re.search(r'\b(?:withdr[ae]w\w*|swap\w*|convert\w*|move|take\s+out|pull|sell|close)\b', msg[:bm.start(1)], re.I):
        return None                                  # "withdraw from aftermath then deposit in suilend best": a chain (2026-10-21)
    if bm and len(p['coins']) == 1 and not msg.strip().endswith('?'):   # "deposit all USDC in to suilend best yield market": do it there
        fam = bm.group(3) or bm.group(4) or ''
        rows = [r for r in lend_rows_for(p['coins'][0]) if not r.get('rate_only') and (not fam or r['market'].startswith(fam.lower()))]
        if rows:
            best = max(rows, key=lambda r: float(r['total_apr']))
            amt = AM.first_or_word(bm.group(2))
            amt = (amt + ' of my') if amt and amt.endswith('%') else amt   # a share stays a share (RDR-V5)
            ask = 'lend %s%s on %s' % ((amt + ' ') if amt else '', p['coins'][0], LEND_MKTS.get(best['market'], best['name']))
            r = widget_reply(ask, address, history) or intent_ask(ask, address, history)
            if r:
                r['text'] = '%s pays the most%s right now (%.2f%% a year). ' % (LEND_MKTS.get(best['market'], best['name']), (' on ' + fam.capitalize()) if fam else '',
                                                                              float(best['total_apr'])) + r.get('text', '')
                return r
    return None


def intent_ask(msg, address, history=None):
    """A clarifying question with choices, or None when the message is clear (or not about Kivy's jobs)."""
    _g = guarded(intent_ask, msg, address, history)   # the read guard first (review 2026-10-29)
    if _g is None:
        return guard_info(intent_ask, msg, address, history) if getattr(XW, 'guard_depth', 0) < 3 else None
    if _g != 'go':
        return _g
    if intent_parse(msg)['fixes'] and other_unknown(msg):
        return None                                      # a foreign sentence: no typo guess (2026-10-23)
    if len(msg) > 120:
        return None
    p = intent_parse(msg)
    if re.search(r"\b(?:don'?t|do\s+not|never|stop)\b", msg, re.I):
        return None                                  # "don't swap…, just explain": never act on a no — the AI answers
    if QUESTION_RE.search(msg) and not (p['tools'] == ['price'] and len(p['coins']) == 1):
        return None                                  # a question ("what does lend mean?"): the AI explains; prices still chart
    bl = best_lend(msg, address, history, p)
    if bl:
        return bl
    if re.search(r'\bbest\s+(?:place|rate|yield|market|apy|apr)\b|\bwhere\s+(?:should|can|do)\s+i\s+(?:put|park|lend)\b', msg, re.I) and len(p['coins']) == 1 \
            and not re.search(r'\b(?:withdr[ae]w\w*|swap\w*|convert\w*|move|then|send)\b', msg, re.I):   # several steps: the chain reader (2026-10-21)
        rows = lend_rows_for(p['coins'][0])          # "best place for usdsui?": the lending table, on that coin's tab
        r = widget_reply('lending markets', address, history) if rows else None
        if r and (r.get('widget') or {}).get('type') == 'markets':
            r['widget']['coin'] = rows[0]['asset']
            r['text'] = 'Here is where %s earns the most right now (best first). Tap a market to lend.' % disp(p['coins'][0])
            return r
    real = [next(c for c in STAKED_SUI if c.lower() == t.lower()) for t, _ in p['fixes'] if t.lower() in REAL_COINS]
    if real and not QUESTION_RE.search(msg):          # "buy afSUI…": a real coin Kivy can't trade — never read it as another coin (2026-11-01)
        ok = [c for c in STAKED_SUI if ckey(c) or lend_token(c)]
        return {'text': '%s is a real coin, but Kivy can\'t swap or lend it yet (and won\'t swap you into a different one). Staked SUI that Kivy '
                        'supports: %s.' % (real[0], ', '.join(ok))}
    if p['fixes']:                                    # a typo: show what we think they meant, one tap to run it
        fixed = msg
        for typed, meant in p['fixes']:
            fixed = re.sub(r'\b%s\b' % re.escape(typed), meant, fixed, count=1)
        try:
            xr = xw_parse(fixed, address, history) if not QUESTION_RE.search(msg) else None
            if xr:
                return xr
        except Exception:
            pass
        try:
            ms = send_reply(fixed, address, history) if not QUESTION_RE.search(msg) else None
            if ms is None and SEND_VERB_RE.match(msg):
                ms = send_reply(msg, address, history)    # the "typo" was a word like "fund": read it as written
            if ms:
                if ms.get('plan') or (ms.get('widget') or {}).get('type') == 'choose':
                    raw = send_reply(msg, address, history)    # the words as typed: a different reading means the fix decided it — say so (review P-26)
                    sig = lambda r: json.dumps([(r.get('plan') or {}).get('action'), [o.get('ask') for o in ((r.get('widget') or {}).get('options') or [])]], sort_keys=True, default=str)
                    if raw is None or sig(raw) != sig(ms):
                        ms['text'] = ('I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes'])) + ms.get('text', '')
                return ms
        except Exception:
            ms = None
        try:
            ms = market_switch(fixed, address, history) if not QUESTION_RE.search(msg) else None
        except Exception:
            ms = None
        if ms:                                       # "switch to etherium": just open it (only opens a page, nothing to approve)
            ms['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + ms.get('text', '')
            return ms
        try:                                         # "swp 5 sui to usdc, deposti to perps and lnog sui 3x": the deposit, then the order (scan 2026-11-14 C9)
            dt = deposit_then_trade(fix_chain_typos(fixed), address, history) if not QUESTION_RE.search(msg) else None
        except Exception:
            dt = None
        if dt is not None:
            dt['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + (dt.get('text') or '')
            return dt
        try:                                         # a typo inside a multi-step request: read it fixed — the card still asks for approval (2026-10-21)
            ip = inproto(fixed, address) if not QUESTION_RE.search(msg) else None
            ch = ip if isinstance(ip, dict) else chain_parse(ip or fixed, address) if not QUESTION_RE.search(msg) else None
        except Exception:
            ch = None
        if ch is not None:
            ch['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + (ch.get('text') or '')
            return ch
        try:                                         # a single step: the card it would open typed right, with the reading shown (2026-10-25)
            wr = widget_reply(fixed, address, history) if not QUESTION_RE.search(msg) and not any(t.lower() in REAL_COINS for t, _ in p['fixes']) else None   # "afSUI" is a real coin, not a typo for haSUI: ask
        except Exception:
            wr = None
        if wr and isinstance(wr.get('widget'), dict):
            wr['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + (wr.get('text') or '')
            return wr
        if all(meant in LEND_MARKET_WORDS for _, meant in p['fixes']) and not QUESTION_RE.search(msg) and not getattr(XW, 'typo_depth', 0):
            XW.typo_depth = 1                        # only market names were fixed: the card the fixed words get, reading shown (review P-25)
            try:
                ia = intent_ask(fixed, address, history)
            except Exception:
                ia = None
            finally:
                XW.typo_depth = 0
            if ia and isinstance(ia.get('widget'), dict):
                ia['text'] = 'I read %s. ' % ', '.join('"%s" as "%s"' % f for f in p['fixes']) + (ia.get('text') or '')
                return ia
        return {'text': 'Did you mean "%s"?' % fixed,
                'widget': {'type': 'choose', 'options': [{'label': fixed, 'sub': 'Tap to run it', 'ask': fixed}]}}
    tools, coins = p['tools'], p['coins']
    combo = re.search(r'\b(then|and then|after that)\b', msg, re.I)
    pron = re.search(r'\b(it|that|this|them|those)\b', msg, re.I)
    if pron and len(coins) < 2:
        return None                                  # "lend it", "swap that to usdc": the AI reads the chat to know what "it" is
    if not tools and len(coins) == 2 and len(msg.split()) <= 4:     # "SUI / USDC": which way?
        a, b = coins
        opts = [{'label': 'Swap %s → %s' % (disp(a), disp(b)), 'sub': 'Pay %s, get %s' % (disp(a), disp(b)), 'ask': 'swap %s to %s' % (a, b)},
                {'label': 'Swap %s → %s' % (disp(b), disp(a)), 'sub': 'Pay %s, get %s' % (disp(b), disp(a)), 'ask': 'swap %s to %s' % (b, a)}]
        o = intent_option('price', a, address)
        if o:
            opts.append({'label': o[0], 'sub': o[1], 'ask': o[2]})
        return {'text': 'Which way?', 'widget': {'type': 'choose', 'options': opts}}
    if tools == ['swap'] and len(coins) == 2 and not combo:          # loose swap ("อยาก swap sui to usdc", "swap half my sui to usdc")
        am = AMOUNT_WORD_RE.search(msg)
        cmd = 'swap %s%s to %s' % ((am.group(1) + ' ') if am else '', coins[0], coins[1])
        if cmd.lower() != msg.strip().lower():
            r = reply(cmd, address) if am else widget_reply(cmd, address, history)
            if r is not None and (not am or r.get('plan') or r.get('text')):
                return r
    if tools == ['swap'] and len(coins) == 1 and not combo and not re.search(r'\d', msg):   # "ape into btc" = buy it
        gets = re.search(r'\b(?:buy|get|ape|into)\b|\b(?:to|for)\s+(?:some\s+|the\s+)?%s\b' % re.escape(coins[0]), msg, re.I)   # "i want to swap my sui": the coin to pay with, not to buy (2026-11-13)
        r = widget_reply(('trade %s' if re.search(r'\bape\b', msg, re.I) else 'buy %s' if gets else 'swap my %s' if re.search(r'\b(?:my|all|half)\b', msg, re.I) else 'swap %s') % coins[0], address, history)   # "ape": spot or perps?
        if r is not None:
            return r
    # clear job + at most one coin, said loosely ("i want to earn on my usdsui"): run it as the plain command
    plain = {'lend': 'lend %s', 'withdraw': 'withdraw %s', 'claim': 'claim rewards', 'balance': 'wallet balance', 'price': 'chart %s'}
    if len(tools) == 1 and len(coins) <= 1 and not combo and tools[0] in plain and (tools[0] == 'lend' or not re.search(r'\d', msg)
                                                                                 or (tools[0] == 'withdraw' and not re.search(r'\d', re.sub(r'\d+(?:\.\d+)?\s*%', '', msg)))):
        c = coins[0] if coins else ''
        if '%s' not in plain[tools[0]] or c or tools[0] in ('lend', 'withdraw'):
            cmd = (plain[tools[0]] % c if '%s' in plain[tools[0]] else plain[tools[0]]).strip()
            if tools[0] == 'lend' and not c:
                cmd = 'lending markets'
            if tools[0] == 'withdraw' and c and re.search(r'\b(?:to|into|as|for)\s+%s\b' % re.escape(c), msg, re.I):
                cmd = 'withdraw to %s' % c           # "cash out to usdc": USDC is what you get, not the coin to look for
            mw = re.search(r'\b((?:suilend|navi)(?:\s+(?:main|ember|matrix\w*|high\s*yield|prime))?|ember|matrix\w*|high\s*yield|prime)\b', msg, re.I) \
                if tools[0] in ('lend', 'withdraw') and c else None
            am = AMOUNT_WORD_RE.search(msg) if tools[0] == 'lend' else None
            if am and c:
                cmd = 'lend %s %s' % (am.group(1), c)
            if mw and c == 'USDC' and tools[0] == 'lend':
                cmd = mw.group(1)                    # USDC's own card: just the market name
            elif mw:
                cmd += (' on %s' if tools[0] == 'lend' else ' from %s') % mw.group(1)
            if cmd.lower() != msg.strip().lower():
                r = widget_reply(cmd, address, history)
                w = (r or {}).get('widget') or {}
                if w.get('type') == 'withdraw':
                    if re.search(r'\bhalf\b|\d\s*%', msg) or AM.unread(msg):
                        w['preset'] = None               # the rewritten command ("withdraw USDsui") preset ALL; the share typed wins (2026-10-31); "a fifth" is never all (RDR-V3)
                    wd_preset(r, msg)                    # "withdraw half my usdc from navi": the rewrite dropped "half"; the user's words set the slider
                if am and w.get('type') == 'lend' and re.match(r'^\d', am.group(1)) and float(w.get('balance') or 0) < 0.01 and w.get('name'):
                    sl = lend_card(w.get('coin') or 'USDC', w['name'], address, float(am.group(1)))   # none of that coin: swap into it and lend (2026-11-01)
                    if sl and (sl.get('widget') or {}).get('type') == 'swaplend':
                        return sl
                if am and w.get('type') == 'lend' and not w.get('preset'):   # "lend 0.1 usdc on navi": keep the amount typed (2026-10-22)
                    w0, mx = am.group(1).lower(), float(w.get('max') or 0)
                    w['preset'] = round(mx * share_said(msg) if share_said(msg) is not None else 0 if re.search(r'\d\s*(?:%|percent\b)', msg, re.I) else
                                        mx if w0 in ('all', 'everything', 'max') else mx / 2 if w0 == 'half' else min(float(w0), mx), 6)
                if w.get('type') == 'lend' and not w.get('preset') and share_said(msg) is not None:   # "lend a quarter of my usdc on ember" (2026-11-13)
                    w['preset'] = round(float(w.get('max') or 0) * share_said(msg), 6)
                if r is not None:
                    return r
    if (len(tools) >= 2 and not combo and len(coins) <= 1) or (not tools and len(coins) == 1 and len(msg.split()) <= 5):
        c = coins[0] if coins else None
        cands = tools if tools else (['withdraw', 'lend', 'swap', 'perps', 'price'])
        opts = []
        for t in cands:
            try:
                o = intent_option(t, c, address)
            except Exception:
                o = None
            if o and all(o[2] != x['ask'] for x in opts):
                opts.append({'label': o[0], 'sub': o[1], 'ask': o[2]})
        if len(opts) >= 2:
            return {'text': ('What would you like to do with %s?' % disp(c)) if c else 'Which one did you mean?',
                    'widget': {'type': 'choose', 'options': opts[:4]}}
        if len(opts) == 1 and not tools:
            return {'text': 'Did you mean this?', 'widget': {'type': 'choose', 'options': opts}}
    return None


def latest_digest(address):
    """The wallet's newest transaction on chain — "show my last transaction" with nothing in the chat yet (2026-10-23)."""
    if not address:
        return ''
    try:
        d = gql('query($a: SuiAddress!) { transactions(last: 1, filter: {sentAddress: $a}) { nodes { digest } } }', {'a': address})
        n = ((d.get('transactions') or {}).get('nodes') or [])
        shadow_note('history', {'a': address, 'n': 1, 'kind': 'sent', 'detail': 0}, n)
        return n[-1]['digest'] if n else ''
    except Exception:
        return ''


# The newest transaction digest mentioned in this chat (for the AI's get_transaction tool).
def last_digest(history):
    for h in reversed(history or []):
        m = re.search(r'transaction ([1-9A-HJ-NP-Za-km-z]{32,44})', str((h or {}).get('text') or ''))
        if m:
            return m.group(1)
    return ''


AI_FAKE_PLAN = re.compile(r"\b(?:i\s+(?:have|'ve)\s+(?:planned|prepared|set\s+up|created|built)|here'?s\s+(?:the|your)\s+(?:plan|transaction)|(?:confirm|approve|sign)\s+(?:it|this|the\s+\w+)?\s*(?:plan\s+|transaction\s+)?in\s+your\s+wallet|do\s+you\s+approve|please\s+(?:confirm|approve))\b", re.I)


def wallet_snapshot(address):
    """A few lines the AI sees with every answer: wallet coins, lent coins, perps — all from Kivy's short caches."""
    out = []
    try:
        hold = all_balances(address) or {}
        coins = sorted(((c, float(v), float(v) * float(coin_px(c) or 0)) for c, v in hold.items() if float(v or 0) > 0), key=lambda x: -x[2])
        coins = [x for x in coins if x[2] >= 0.01]
        out.append('Wallet: ' + (', '.join('%s %s (≈$%.2f)' % (dec(a), disp(c), u) for c, a, u in coins) if coins else 'nothing worth over a cent'))
    except Exception:
        pass
    try:
        lent = ['%s USDC on %s' % (dec(float(p.get('usdc') or 0) + float(p.get('in_app') or 0)), p.get('name') or k)
                for k, p in (lend_positions(address) or {}).items() if float(p.get('usdc') or 0) + float(p.get('in_app') or 0) >= 0.01]
        lent += ['%s %s on %s' % (dec(x['lent']), x['coin'], x['name']) for x in other_lent(address)]
        out.append('Lent: ' + (', '.join(lent) if lent else 'nothing'))
    except Exception:
        pass
    try:
        r = lend_cli('perp-positions', address, timeout=40)
        if r.get('has'):
            ps = ['%s %s %gx, size %s, price PnL $%.3f, funding $%.3f, net $%.3f, liquidation %s' % (   # net = price PnL + funding since it opened
                      p['symbol'], p['side'], float(p.get('leverage') or 0), dec(p['size']), float(p.get('pnl') or 0), float(p.get('funding') or 0),
                      float(p.get('pnl') or 0) + float(p.get('funding') or 0), fmt_px(p['liquidation']) if p.get('liquidation') else '?') for p in (r.get('positions') or [])]
            out.append('Perps: %s USDC free, equity $%.2f%s' % (dec(float(r.get('free') or 0)), float(r.get('equity') or 0), ('; positions: ' + '; '.join(ps)) if ps else '; no positions'))
        else:
            out.append('Perps: no account yet')
    except Exception:
        pass
    return '\n'.join('- ' + x for x in out)


PLAIN_COIN_WORDS = {'stablecoin': 'USDC', 'stablecoins': 'USDC', 'stable': 'USDC', 'stables': 'USDC', 'dollars': 'USDC', 'dollar': 'USDC',
                    'usd': 'USDC', 'bitcoin': 'BTC', 'ether': 'ETH', 'ethereum': 'ETH', 'solana': 'SOL'}
PLAIN_NOT_COINS = {'send', 'up', 'treat', 'blue', 'fud', 'strat', 'me'}   # coin symbols that are also everyday words


def plain_command(msg):
    """A plain sentence that clearly asks to move money -> the command Kivy's code reads ("move 0.05 of my sui into a stablecoin"
    -> "swap 0.05 SUI to USDC"). Used when the AI may not see the wallet (Settings › Privacy), so the card comes from code
    (2026-10-27). None when anything is unclear: the AI answers as before."""
    try:
        if read_guard(msg, None) is not None:       # except / not / if / what if / $N of …: never turned into a plain command (review 2026-10-29)
            return None
    except Exception:
        return None
    low = ' '.join(str(msg or '').lower().split())
    rc = re.search(r'\b(0x[0-9a-f]{64}|[a-z0-9-]+\.sui)\b', low)
    rest = low.replace(rc.group(1), ' ') if rc else low
    amt = AM.first_or_word(rest, ('all', 'half', 'everything'))   # the amount reader's reading: never a count, a date, a reference number
    if not amt:
        return None
    amt = (amt + ' of my') if amt.endswith('%') else amt   # a share stays a share (RDR-V5)
    syms = {k.lower(): k for k in ecoins()}
    coins = []
    for w in re.findall(r'[a-z][a-z0-9.]*', rest):
        w = w.rstrip('.')
        c = PLAIN_COIN_WORDS.get(w) or (syms.get(w) if w not in PLAIN_NOT_COINS else None)
        if c and c not in coins:
            coins.append(c)
    if not coins:
        return None
    proto = re.search(r'\b(navi|suilend|scallop|ember|alphalend)\b', rest)
    on = (' on ' + proto.group(1)) if proto else ''
    if rc and re.search(r'\b(send|pay|transfer|give)\b', rest):
        return 'send %s %s to %s' % (amt, coins[0], rc.group(1))
    if re.search(r'\b(withdraw|take out|pull out|take back|get back)\b', rest) and len(coins) == 1:
        return 'withdraw %s %s%s' % (amt, coins[0], (' from ' + proto.group(1)) if proto else '')
    if re.search(r'\b(lend|deposit|supply|earn|to work|interest|yield)\b', rest) and len(coins) == 1:
        return 'lend %s %s%s' % (amt, coins[0], on)
    if len(coins) == 2 and re.search(r'\b(swap|convert|move|switch|change|exchange|turn|sell|buy|put)\b', rest):
        a, b = (coins[1], coins[0]) if re.search(r'\bbuy\b', rest) and re.search(r'\bwith\b', rest) else (coins[0], coins[1])
        return 'swap %s %s to %s' % (amt, a, b)
    return None


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import chart_symbol, fmt_px  # noqa: E402
from .situations import REAL_COINS, STAKED_SUI  # noqa: E402
from .readguard import guard_info, guarded, read_guard  # noqa: E402
from .wordings import deposit_then_trade  # noqa: E402
from .chainreads import all_balances, ckey, dec, ecoins, gql, shadow_note  # noqa: E402
from .fallback import reply  # noqa: E402
from .lending import (  # noqa: E402
    LEND_MKTS, coin_px, lend_card, lend_positions, lend_rows_for, lend_token, market_only, other_lent,
    wd_preset)
from .chains import chain_parse, fix_chain_typos, inproto  # noqa: E402
from .cards import share_said  # noqa: E402
from .marketnames import market_switch  # noqa: E402
from .sends import SEND_VERB_RE, XW, send_reply, xw_parse  # noqa: E402
