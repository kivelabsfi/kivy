"""Chat patterns: sentence shapes for lending, rewards and claim, withdraw, perps margin (Section 3).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# Read by widget_reply (server.py) and the perps readers; pure text, no state but the _PERP cache.
import re
from . import perpwords as PERPWORDS


# ── Section 3: chat patterns — exact sentence shapes for lending tables, rewards/claim, withdraw, perps deposit/withdraw;
#    read by widget_reply / perp_widget (more patterns sit next to the functions that use them) ──
# Shared pieces that used to be written out at several call sites (review code-waste CW-3, 2026-11-03): one copy to fix.
TICKER_WORD_RE = re.compile(r'[A-Za-z&][A-Za-z0-9&]{1,9}')                  # a ticker-like word, US500 / S&P too
MKT_SPACE_RE = re.compile(r'\b([A-Za-z]{2,4})\s+(\d{2,4})\b')                 # "US 500", "JP 225": a market name with a space
SWAP_VERB_RE = re.compile(r'^\s*(?:swap|convert|trade|exchange)\b', re.I)
ADDR_NAME_RE = re.compile(r'0x[0-9a-f]+|\b[a-z0-9]{20,}\b|@[\w.-]+|\b[\w-]+\.sui\b')   # addresses and names (not language; lower-cased text)
LABEL_BAD_RE = re.compile(r'[^A-Za-z0-9 _.,:@$%/+()→≈…\-]')                 # characters a stored activity label may not keep
SUILEND_MKT_RE = re.compile(r'\bsuilend\b|\bember\b|\bmatrixdock\b')
PLAIN_NUM_RE = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)(?![\w.%])')            # a bare number, not part of a word, decimal or percent
TRADE_LEV_RE = re.compile(r'(\d+(?:\.\d+)?)\s*x\b')                          # parse_trade: leverage, size and limit price
TRADE_SIZE_RE = re.compile(r'(?:\$\s*(\d+(?:\.\d+)?)(?![\d.]|\s*x)|(\d+(?:\.\d+)?)\s*(?:\$|usdc?\b|dollars?\b)|\bfor\s+(\d+(?:\.\d+)?)\b(?![\d.]|\s*x))')   # a number is never cut short ("$10 x3" was $1, RDR-V7)   # "5 usdc" too (2026-10-24)
TRADE_PX_RE = re.compile(r'(?:\blimit\s+(?:at\s+)?|\bat\s+(?:a\s+price\s+of\s+)?|@\s*|\bwhen\s+it\s+(?:hits|reaches)\s+)\$?\s*(\d+(?:\.\d+)?)(?!\s*(?:x|\$|usd|dollars?)\b)')
LEND_ASK_RE = re.compile(r'\b(?:differen\w*|safe\w*|risk\w*|why|vs|versus|which\s+is\s+better|collateral|liquidat\w*|bad\s+debt)\b', re.I)   # not 'explain': "explain APR vs APY" is a lesson
MARKETS_RE = re.compile(r"^\s*(?:i\s+want\s+to\s+|let'?s\s+|can\s+i\s+)?(?:lend|lending|earn|yield|deposit|supply)(?:\s+(?:my\s+)?usdc)?\s*[?.!]*\s*$|\blending\s+(markets?|rates?|options?|yields?)\b|\bwhere\s+(can|should|do)\s+i\s+lend\b|\bcompare\s+(lending|yields?|rates?)\b|\bbest\s+(lending\s+)?(yield|rate|apy|apr)\b", re.I)


REWARD_Q_RE = re.compile(r'^\s*(?:how\s+much|what|any|do\s+i\s+have|did\s+i\s+earn)\b[^.?!]{0,30}\brewards?\b[^.?!]{0,25}[?.!]*\s*$|\bworth\s+claiming\b', re.I)
CLAIM_SCOPES = {'all': '', 'p-suilend': 'Suilend', 'p-navi': 'NAVI'}   # claim everything, or one protocol's markets (2026-10-25)
CLAIM_PROTO_RE = re.compile(r'^\s*claim\s+(?:all\s+)?(?:my\s+)?(?:the\s+)?(suilend|navi)(?:\s+(?:lending\s+)?rewards?)?\s*[?.!]*\s*$|^\s*claim\s+(?:on|from|at)\s+(suilend|navi)\s*[?.!]*\s*$|^\s*claim\s+(?:my\s+)?(?:lending\s+)?rewards?\s+(?:from|on)\s+(suilend|navi)\s*[?.!]*\s*$', re.I)
CLAIM_RE = re.compile(r'^\s*(?:claim\s+(?:it\s+all|everything|all\s+of\s+(?:them|it))|claim(?:\s+(?:my|all))?(?:\s+(?:lending\s+)?rewards?)?|(?:my\s+)?(?:lending\s+)?rewards?|show\s+(?:my\s+)?rewards?)\s*[?.!]*\s*$', re.I)


WITHDRAW_RE = re.compile(r'^\s*(?:i\s+want\s+to\s+|let\'?s\s+|can\s+i\s+)?(?:withdraw(?:al)?|redeem|take\s+(?:out|back)|cash\s+out)(?:\s+(?:it\s+)?(?:all|everything))?'
                         r'(?:\s+(?:from\s+)?(?:my\s+)?(?:usdc|money|funds|lending|lent|lended|loans?|deposits?)(?:\s+usdc)?)?\s*[?.!]*\s*$', re.I)
WITHDRAW_FROM_RE = re.compile(r'^\s*(?:withdraw|redeem|take\s+out)\s+(?:(?:all|the|my|of|everything)\s+)*(?:usdc\s+)?(?:from|on|in|out\s+of)\s+(.+?)\s*[?.!]*\s*$', re.I)   # + "withdraw USDC from Suilend Ember" (2026-10-22)


PERP_WORD = PERPWORDS.PERP_WORD                    # the perps account in every word chat and chains accept, common typos too (one list, 2026-11-14)
PERP_RE = re.compile(r'^\s*(?:(?:show|see|check|view)\s+(?:me\s+)?)?(?:my\s+|the\s+|open\s+(?:a\s+)?)?' + PERP_WORD + r'(?:\s+(?:account|balance|margin|collateral|equity))?\s*[?.!]*\s*$|^\s*(?:my\s+)?aftermath(?:\s+(?:account|balance))?\s*$'
                     r'|^\s*(?:show\s+)?(?:my\s+)?(?:perps?\s+|aftermath\s+)?(?:margin|collateral)\s+balance\s*[?.!]*\s*$', re.I)
PERP_DEP_RE = re.compile(r'^\s*(?:deposit|add|fund|top\s*up|put)(?:\s+(\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?|all|max|half))?(?:\s+(?:usdc|of\s+my\s+usdc|my\s+usdc))?\s+(?:to|into|in|on)\s+(?:my\s+)?' + PERP_WORD + r'\s*[?.!]*\s*$', re.I)
PERP_WD_RE = re.compile(r'^\s*(?:withdraw|take\s+out|remove|pull)(?:\s+(\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?|all|max|half|everything))?(?:\s+(?:usdc|my\s+usdc))?\s+(?:from|out\s+of)\s+(?:my\s+)?' + PERP_WORD + r'\s*[?.!]*\s*$', re.I)
_PERP = {}
