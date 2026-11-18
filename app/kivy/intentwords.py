"""The words the intent reader knows: jobs, verbs, coin names, question shapes, stop words (Section 9).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# Plain constants, read by the readers at import time too (pieces, marketnames), so they live apart from intent.py.
import re


# ── Section 9: intent — which job, coin and market a message means; asks with tappable choices when more than one fits.
#    intent_ask() is called by /api/chat and widget_reply; wallet_snapshot() feeds the AI prompt ──
# ── What does the user mean? job -> coin -> market; ask when more than one fits (2026-10-20) ──
# Runs after the exact command patterns and before the AI. Typos are forgiven ("witdraw", "usdsiu"), and a message
# that names a coin but no job (or two jobs) gets tappable choices built from the user's own wallet.
INTENT_VERBS = {
    'swap': ['swap', 'convert', 'exchange', 'change', 'buy', 'sell', 'ape'],
    'lend': ['lend', 'supply', 'deposit', 'earn', 'yield', 'park', 'stash'],
    'withdraw': ['withdraw', 'redeem', 'unlend'],
    'send': ['send', 'transfer', 'pay'],
    'claim': ['claim', 'harvest', 'rewards'],
    'perps': ['long', 'short', 'leverage', 'perps', 'perp', 'futures'],
    'price': ['price', 'chart'],
    'balance': ['balance', 'holdings', 'portfolio'],
}
VERB_OF = {w: t for t, ws in INTENT_VERBS.items() for w in ws}
COIN_NAMES = {'bitcoin': 'BTC', 'btc': 'BTC', 'ethereum': 'ETH', 'ether': 'ETH', 'solana': 'SOL', 'walrus': 'WAL', 'deepbook': 'DEEP',
              'usd': 'USDC', 'dollar': 'USDC', 'dollars': 'USDC', 'stablecoin': 'USDC'}
# "if …, can I / will it / how much …?" (a conditional question, ending in ?): a question, never an order (2026-11-10:
# "if NAVI is paused can I still withdraw?" got the withdraw rule, "if I open a $10 long at 4x … borrowed?" the borrow one)
_IF_Q = (r"^\s*(?:if|what\s+if|suppose|say)\b[^?]{0,200}?\b(?:(?:can|could|will|would|should|do|does|did|is|are|am)\s+"
           r"(?:i|it|my|you|kivy|they|we|that|this)\b|how\s+(?:much|many|long|far))[^?]*\?\s*$")
IF_Q_RE = re.compile(_IF_Q, re.I)
QUESTION_RE = re.compile(r"^\s*(?:what\s+(?:does|is|are|do|would|will|fees?)|what's|whats|how\s+(?:does|do|is|much|many|long)|why|explain|meaning\s+of)\b|\bmean\??\s*$"
                         r"|^\s*(?:do|does|am|is|are|can|could|should|would|will|who|when|which)\s+\w+.*\?\s*$"
                         r"|" + _IF_Q, re.I)
AMOUNT_WORD_RE = re.compile(r'\b(\d+(?:\.\d+)?|all|half|max|everything)\b', re.I)
INTENT_FORMS = set('lent lended lending swapped swapping sent sending withdrew withdrawn withdrawing claimed claiming deposited '
                   'depositing bought selling sold earned earning paid parked'.split())
# everyday words that must never be "corrected" into a coin ('keep' is one letter from DEEP — 2026-10-20)
INTENT_FORMS |= set('keep leave hold take use put move shift pull push give rest half margin collateral aftermath perps perp wallet '
                    'proceeds remaining remainder everything whole other plus minus then after before into onto from with without '
                    'market markets account position split part parts each both first second rewards balance chart price pool '
                    'safe best good high yield prime main ember navi suilend some most less more liquid deposit there here '
                    'whale whales close cancel flip profit loss stop entry exposure leverage leveraged start started bot bots trading '
                    'park those these them send keep fund funds money asset assets crypto holdings whole entire '
                    'well along including plus together remaining remain leftover'.split())
INTENT_STOP = set('up so it as to in on of or and my me the an is be do you all max half some any what about how please pls then '
                  'with for from into at can want wanna would like need now this that coin coins token tokens money out back take '
                  'put get much many more less best yes no ok okay hi hello thanks thank sure maybe just also too'.split())


ENGLISH_ONLY = ('Kivy understands English only for now. Please write your request in English — for example '
                '"lend 10 USDC", "swap 5 SUI to USDC" or "withdraw my USDsui".')
