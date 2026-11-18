"""The read guard (one pre-pass before every reader) and the scam and rule-change warnings (Section 6).

Moved verbatim out of site/server.py (server split 2, 2026-11-14); server.py imports every name back, so callers
and tests that use them through server.py (S.name, kivy.seam) are unchanged."""
# How it runs: imported by site/server.py inside kivy-site; nothing here runs on its own.
# guarded / guard_info / withdraw_coverage are run by widget_reply, intent_ask and reply(); scam_warn by code_answer.
# Imports: stdlib, settings, the earlier kivy modules and the leaf modules (config, patterns, intentwords) at the top;
# the other reader modules at the END (they call each other in a cycle — see the note there).
import math
import re
from . import amounts as AM
from .config import disp
from .intentwords import IF_Q_RE, QUESTION_RE
from .patterns import SWAP_VERB_RE


# ── Read guard (review user-harm review 2026-10-29, P-1…P-33): ONE pre-pass before every reader ──
# The readers take the first number / coin / market of a sentence and never look at the words around it, so "send everything
# except SUI", "swap $5 of BTC", "lend 5 USDC, not on Suilend", "what if I swap…" and "i have 20 sui, swap 5" built the wrong
# card. Here a money sentence like that is either rewritten into a form the readers already build (said back: "Read …") or
# ASKED — never guessed. read_guard() -> None (go on) | a reply | {'_rewrite', '_say'} | GUARD_AI (no card from code: the AI
# answers, and its plan tools refuse the same sentences). Run first by widget_reply, intent_ask and reply() (typo_chain reads
# through widget_reply), so every path sees it.
GUARD_AI = {'_guard': 'ai'}
G_INFO_CARDS = ('choose', 'perppos', 'chart', 'positions', 'markets', 'perpmarkets', 'perpacct', 'mktview')   # nothing to sign on these
G_VERB = r"(?:swap|convert|trade|exchange|send|transfer|pay|give|lend|supply|deposit|earn|put|withdraw|redeem|move|shift|buy|sell|long|short|stake|unstake|bridge)"
G_VERB_RE = re.compile(r'\b%s\b' % G_VERB, re.I)
G_SPOT_RE = re.compile(r'\b(?:swap|convert|exchange|send|transfer|pay|give|lend|supply|deposit|earn|withdraw|redeem|stake|unstake)\b', re.I)
G_PERP_RE = re.compile(r'\b(?:long|short|perps?|perpetuals?|leverage|margin|collateral|aftermath|liquidat\w*)\b|\d\s*x\b', re.I)
G_HYPO_RE = re.compile(r"^\s*(?:(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:explain|describe|summari[sz]e)|what\s+if|what\s+happens|what\s+would\s+happen|is\s+it\s+(?:a\s+)?(?:good|bad|smart|wise|safe|ok|okay|worth|better|risky)"
                       r"|would\s+it\s+be|(?:can\s+you\s+)?(?:guarantee|promise)|should\s+i|shall\s+i|do\s+you\s+think|did\s+i|have\s+i|was\s+it|suppose|imagine|hypothetically)\b"
                       r"|^\s*i\s+(?:already\s+|just\s+)?(?:sent|swapped|lent|withdrew|deposited|bought|sold|transferred|staked|traded)\b", re.I)
# a negation in any spelling ("don't", "won't", "can't", "shouldn't", "didn't", "do not", "never"); hygiene.norm_words has made a
# phone's ’ into ' and "wont" into "won't" first (QA 2026-11-17, RDR-V6: "don’t swap…" and "i won't swap…" built the swap)
G_NOT = r"(?:don'?t|dont|do\s+not|never|won't|will\s+not|can't|cannot|can\s+not|shouldn't|should\s+not|wouldn't|would\s+not|didn't|did\s+not|mustn't|must\s+not|couldn't|could\s+not)"
G_NEG_RE = re.compile(r"^\s*(?:(?:please|pls)\s+)?(?:(?:i|we)\s+(?:(?:really|just|definitely|honestly|certainly)\s+)?)?(?:%s|no\s+way\s+(?:i|we)(?:\s+(?:would|will|am\s+going\s+to|gonna))?|(?:i|we)\s+refuse\s+to)\s+"
                      r"(?:(?:(?:really\s+)?(?:mean|want|need|plan|intend|wish|like)\s+)?(?:you\s+)?to\s+)?(?:ever\s+)?%s\b" % (G_NOT, G_VERB), re.I)
G_NEG_ANY_RE = re.compile(r"\b%s\s+(?:ever\s+)?%s\b" % (G_NOT, G_VERB), re.I)
G_COND_RE = re.compile(r"\b(?:if|when|whenever|once|unless|as\s+soon\s+as)\b", re.I)
CANCEL_RE = re.compile(r"\b(?:scrap|scratch|cancel|forget|drop)\s+(?:the\s+whole\s+thing|that|it\s+all|all\s+of\s+(?:it|that)|everything)\b|\bnever\s*mind\s*[,.!]?\s*(?:leave|don'?t|forget|$)"
                       r"|(?:[,;\u2014\u2013-]|\.\.\.)\s*(?:no|nah|nope)\s*[,.!]?\s*(?:don'?t|do\s+not)(?:\s+do\s+(?:it|that))?\s*[.!]*\s*$"   # "send 5 usdc to bob.sui — no, don't" (RDR-V6)
                       r"|\bleave\s+every\s+coin\s+alone\b|\b(?:no\s+wait|wait,?\s+no|actually)\s*,?\s*(?:leave\s+it(?:\s+(?:alone|be))?\s*[.!]*\s*$|don'?t(?:\s+do\s+it)?\s*[.!]*\s*$|forget\s+it|never\s*mind|stop)", re.I)
CORRECT_RE = re.compile(r"\S[^.!?]*?[,;—–-]\s*(?:actually|correction|no\s+wait|wait,?\s+no|on\s+second\s+thought|no,\s+make\s+(?:it|the))\b"
                        r"|\b(?:correction|on\s+second\s+thought)\s*[,:]", re.I)
ADVICE_Q_RE = re.compile(r"\b(?:should\s+i|shall\s+i|is\s+(?:it|that|this)\s+(?:a\s+)?(?:good|bad|smart|wise|safe|risky)(?:\s+(?:idea|move|plan))?|good\s+idea|worth\s+it|is\s+that\s+legit)\s*\?+\s*$"
                         r"|\bactually\s*,?\s*(?:is|are|should|would|do|does|can|could|what|how|why)\b[^?]*\?\s*$", re.I)
STORY_RE = re.compile(r"\b(?:(?:her|his|their|a|my\s+friend'?s?)\s+story|telling\s+you\s+(?:a|her|his|their|about)|not\s+(?:giving\s+)?(?:an?\s+)?(?:instructions?|orders?|requests?)\b"
                      r"|not\s+approving|i'?m\s+reporting|reporting\s+(?:that|this|a)\s+(?:message|dm)|(?:scam|the)\s+dm\s+(?:reads|says)|a\s+story\s+about"
                      r"|(?:the\s+)?message\s+(?:reads|said|says)\s*['\"“‘]|someone\s+impersonating|do\s+not\s+follow\s+(?:the|this|that|it)"
                      r"|describing\s+what\s+happened|not\s+asking\s+(?:for|you\s+to)|(?:as\s+an?\s+|for\s+)?example\s+only|just\s+an?\s+example|^\s*imagine\b|demo\s+wallet|copied\s+from\s+(?:our|a|the|my)\s+(?:chat|conversation|group|thread)|chat\s+log|old\s+draft"
                      r"|\b(?:rewrite|rephrase)\s+(?:it|this|that)|(?:^|\n)\s*>\s*\S)", re.I)
COND_Q_RE = re.compile(r"\b(?:what|how\s+much|how\s+many|will|would|do|does|did|can|could|is|are|should)\s+(?:i|it|you|kivy|that|this|my|there)\b[^?]*\?\s*$", re.I)
POLITE_RE = re.compile(r"\b(?:whenever|when)\s+(?:you(?:'re|\s+are)\s+ready|you\s+(?:get|have)\s+a\s+(?:chance|moment|sec(?:ond)?|minute))\b"
                       r"|\bif\s+(?:it'?s|it\s+is)\s+not\s+too\s+much\s+trouble\b|\bif\s+you\s+(?:don'?t|do\s+not)\s+mind\b|\bif\s+(?:you\s+)?(?:can|could|possible)\b"
                       r"|^\s*(?:if\s+you\s+(?:don'?t|do\s+not)\s+mind|if\s+possible|whenever\s+you\s+(?:can|get\s+a\s+chance))\s*,?\s*", re.I)
# "what exactly is your own charge", "just a price, not an approval", "a quote … without moving it": a question, never a card to sign
INFO_Q_RE = re.compile(r"\b(?:your|kivy'?s)\s+(?:own\s+)?(?:fee|charge|cut)\b|\b(?:does|do|will|would)\s+(?:kivy|you)\s+charge\b|\bjust\s+a\s+(?:price|quote)\b"
                       r"|\bnot\s+(?:an?\s+)?approval\b|\b(?:see|get|give\s+me)\s+a\s+quote\b"
                       r"|\bwould\s+(?:become|turn\s+into|arrive)\b|\b(?:just|only)\s+compare\b|\bcompare\s+only\b|\basking\s+for\s+a\s+number\b|\bnot\s+authori[sz]ing\b"
                       r"|\b(?:confirmed|went\s+through|still\s+not\s+through)\b|\bservice\s+charge\b|\bfree\s+on\s+(?:your|kivy'?s)\s+side\b|\b(?:would|will|does|do)\s+(?:kivy|you)\s+(?:take|keep|add)\s+(?:anything|something|a\s+cut|extra)\b|\b(?:i'?m|i\s+am)\s+not\s+(?:asking|looking|trying|going)\s+to\s+(?:buy|sell|trade|swap)\b", re.I)
G_EXCEPT_RE = re.compile(r"\b(?:except|excluding|exclude|apart\s+from|other\s+than|besides|but\s+not|anywhere\s+but|everywhere\s+but|all\s+but|everything\s+but"
                         r"|without|instead\s+of)\b|\bnot\b(?!\s+(?:enough|sure|yet|too|much|more|less|only|just)\b)|\b(?:don'?t|dont|do\s+not|never|won't|shouldn't|mustn't)\b", re.I)
G_EXCEPT_OK = re.compile(r"\bbut\s+keep\s+(?:it|them)\b|\bwithout\s+(?:changing|moving|leaving)\b", re.I)
G_KEEP_WD = re.compile(r"^\s*(?:withdraw|take\s+out|pull(?:\s+out)?|redeem|take)\b(?:(?!\b(?:and|then)\b)[^,;])*?\b(?:except|but\s+keep|leaving|leave|keeping)\s+\$?\s*\d", re.I)   # the withdrawal's own clause only (RDR-V9)
G_USD_OF = re.compile(r'(?:\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:usd|dollars?|bucks)\b)\s+(?:(worth)\s+)?(?:(of)\s+)?(?:my\s+|the\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b', re.I)
G_PAIR_RE = re.compile(r'(\d+(?:\.\d+)?|all|half)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:to|for|into)\s+([A-Za-z][A-Za-z0-9]{1,9})\b', re.I)
G_HAVE_RE = re.compile(r"(?:^|(?<=[,;.!])|\b(?:and|but|so)\b)\s*\(?\s*i\s+(?:have|hold|own|got|'ve\s+got|have\s+got|currently\s+have)\s+(?:only\s+|about\s+|around\s+|like\s+)?"
                       r"(\d+(?:\.\d+)?)\s*([A-Za-z][A-Za-z0-9]{1,9})?(?:\s+in\s+my\s+wallet)?\s*\)?\s*(?:[,;.]|$)", re.I)
G_BAD_MKT = re.compile(r'\b(?:on|in|at|into|with|via|to)\s+(scallop|aave|bluefin|cetus|kai|alphafi|alphalend|bucket|haedal|turbos|momentum|typus|kriya|flowx|springsui|deepbook)\b', re.I)
G_LEND_MKT = r'(?:navi|suilend|ember|matrix\w*|prime|high\s*yield)(?:\s+(?:main|prime|ember|market|high\s*yield|sui\s+prime))*'
G_STOP = {'to', 'my', 'all', 'of', 'the', 'and', 'it', 'on', 'in', 'at', 'for', 'from', 'into', 'with', 'half', 'max', 'worth', 'some', 'a', 'an', 'then', 'rest',
          'send', 'swap', 'lend', 'buy', 'sell', 'give', 'pay', 'move', 'put', 'keep', 'other', 'earn', 'deposit', 'withdraw', 'long', 'short', 'trade'}
G_STABLE = ('USDC', 'USDsui', 'USDT', 'wUSDC', 'suiUSDT', 'USDY', 'AUSD', 'FDUSD', 'BUCK')
G_SAY_IT = ('I won\'t guess what to leave out or where not to go with money. Say exactly what to do — one coin, one amount, one place — '
            'e.g. "send 5 USDC to alice.sui" or "lend 5 USDC on NAVI" — and I\'ll prepare it.')


def _gcoin(w):
    """A coin word in a guard pattern (never an English filler word)."""
    return None if not w or w.lower() in G_STOP else (ckey(w) or lend_token(w))


def _gnum(x):
    """A number as people read it: 0.0000624, 12.5, 1000 — never 6.24e-05."""
    x = float(x)
    if x <= 0:
        return '0'
    t = '%.*f' % (min(12, max(0, 6 - 1 - int(math.floor(math.log10(x))))), x)
    return t.rstrip('0').rstrip('.') if '.' in t else t


# ── Scam and rule-change warnings (2026-10-31, from the a reviewer/a reviewer scenario round): answered by code, before any reader or
# the AI. "support told me to send … to verify", "double my sui", "ignore your rules", "admin mode", "SYSTEM: pre-approved". Seed
# questions keep their own "Never share" answer; automation requests keep theirs.
SCAM_INJECT_RE = re.compile(
    r"\bignore\s+(?:all\s+|any\s+|every\s+)?(?:(?:of\s+)?(?:your|the|previous|prior|above|earlier|these|those|kivy'?s?)\s+)*(?:rules|instructions|guidelines|limits|restrictions|prompts?|safety|checks)\b"
    r"|\b(?:admin|developer|dev|god|debug|maintenance|root|sudo|jailbreak|unrestricted|dan)\s+mode\b"
    r"|^\s*\[?\s*(?:system|assistant|developer|admin)\s*\]?\s*:"
    r"|\b(?:pre-?approved|already\s+approved|approved\s+by\s+(?:kivy|the\s+team|admin|support|the\s+developer))\b"
    r"|\bskip\s+(?:the\s+|my\s+|all\s+)?(?:approval|confirmation|wallet\s+pop-?up|checks?|guard|safety)\b"
    r"|\b(?:without|no\s+need\s+for)\s+(?:asking\s+me|my\s+approval|approval|confirmation|confirming|signing)\b"
    r"|\bi\s*(?:'m|\s+am)\s+(?:the|kivy'?s?|your)\s+(?:lead\s+)?(?:developer|dev|admin|administrator|creator|founder|moderator)\b"
    r"|\b(?:disable|turn\s+off|switch\s+off|bypass|remove|override)\s+(?:the\s+|your\s+|all\s+)?(?:gas\s+reserve|guard|checks?|safety|limits?|allowlist|rules|filters?)\b"
    r"|\b(?:show|reveal|print|dump|repeat|tell)\s+(?:me\s+)?(?:your\s+|the\s+)?(?:system\s+prompt|hidden\s+prompt|initial\s+prompt)\b|\b(?:show|reveal|print|dump|repeat)\s+(?:me\s+)?your\s+(?:instructions|rules|prompt)\b"
    r"|\b(?:disregard|forget|override|bypass)\s+(?:all\s+|any\s+|every\s+)?(?:(?:of\s+)?(?:your|the|previous|prior|above|earlier|these|those|kivy'?s?)\s+)*(?:rules|instructions|guidelines|limits|restrictions|prompts?|safety|checks)\b"
    r"|^\W*(?:system|admin|developer)\s+(?:override|message|notice|prompt|instructions?)\b|</?\s*(?:tool_result|tool|system|function_result|function|assistant)\s*>"
    r"|\bpre-?authori[sz]ed\b|\bpre-?sign\w*|\bauto-?approv\w*|\bapproval\s+granted\b|\b(?:owner|developer|creator|team|founder)s?\s+of\s+kivy\s+(?:has\s+)?approved\b"
    r"|\bnew\s+instructions?\s+from\s+(?:the\s+)?(?:developer|admin|owner|team|kivy|system)\b|\bset\s+the\s+recipient\s+of\s+(?:every|all)\b"
    r"|\bas\s+(?:the|my)\s+fee\s+(?:wallet|address|recipient)\b|\bfee\s+(?:wallet|address|recipient)\s+(?:for|to)\s+(?:all|every)\b"
    r"|\b(?:skip|without)\s+(?:a\s+|the\s+)?card\b|\bdon'?t\s+show\s+(?:me\s+)?(?:the\s+|a\s+)?card\b", re.I)
SCAM_SEND_RE = re.compile(
    r"\b(?:support|admin|moderator|mods?|team|staff|agent|stranger|telegram|discord|twitter|whatsapp|customer\s+service|help\s*desk|kivy|sui\s+foundation|validator|wallet\s+team|recovery\s+(?:service|team|agent))\b"
    r".{0,60}\b(?:told|asked|said|wants?|instructed|needs?|requires?|says|tells?|dm'?d|messaged|contacted|emailed|wrote)\b.{0,40}\b(?:send|transfer|deposit|pay|connect|verify|validate|sync|approve|sign)\b"
    r"|\b(?:send|transfer|deposit|pay)\b.{0,60}\bto\s+(?:verify|validate|sync|unlock|activate|unfreeze|whitelist|rectify|reactivate|repair|restore|secure|fix)\b|\b(?:send|transfer|deposit|pay)\b.{0,60}\bfor\s+(?:verification|validation|kyc|activation|unlocking)\b"
    r"|\b(?:send|transfer|move)\b.{0,60}\bto\s+(?:our|the|a|this)\s+(?:recovery|safe|secure|backup)\s+(?:wallet|address|vault)\b"
    r"|\b(?:double|2x|triple|3x)\s+(?:my|your)\s+(?:sui|usdc|coins?|money|crypto|funds)\b|\bgiveaway\b"
    r"|\bsend\b.{0,40}\b(?:get|receive)\b.{0,25}\b(?:back\s+double|double|twice|2x|3x)\b"
    r"|\bsend\b.{0,40}\bget\b.{0,15}\b\d+(?:\.\d+)?\s*(?:[a-z]+\s+)?back\b"
    r"|\b(?:recover|get\s+back)\s+(?:my\s+)?(?:stolen|lost|hacked|scammed)\b.{0,60}\b(?:fee|pay|send|deposit)\b"
    r"|\bairdrop\b.{0,80}\b(?:claim|connect|receive)\b|\b(?:claim|free)\b.{0,40}\bairdrop\b.{0,80}\b(?:connect|link|site|at)\b"
    r"|\bsend\b.{0,80}\b(?:sends?|gives?|returns?|pays?)\s+(?:you\s+|me\s+)?(?:\d+(?:\.\d+)?\s*(?:[a-z]+\s+)?)?back\b"
    r"|\b(?:pay|send|deposit)\b.{0,40}\bto\s+(?:release|unlock|unfreeze)\s+(?:them|it|my|your|the)\b"
    r"|\b(?:compromised|hacked|drained|at\s+risk)\b.{0,80}\b(?:move|send|transfer)\b.{0,60}\b(?:safe|secure|backup|recovery)\s+(?:address|wallet|vault)\b", re.I)
SCAM_SEED_RE = re.compile(r'\b(?:seed|recovery\s+phrase|secret\s+phrase|private\s+key|mnemonic)\b', re.I)


def scam_warn(msg):
    """None, or the fixed warning for a scam or a request to change Kivy's rules from chat."""
    m = re.sub(r'0x[0-9a-fA-F]{20,}', '0xADDR', str(msg or ''))   # "send 0.2 sui to 0x…64… to verify" (2026-11-01)
    if SCAM_SEED_RE.search(m):
        return None
    if SCAM_SEND_RE.search(m):
        return SCAM_WARN
    if SCAM_INJECT_RE.search(m) and (not re.search(r'\b(?:every|daily|weekly|monthly|automatic(?:ally)?|schedule[sd]?|recurring|each\s+(?:day|week|month))\b', m, re.I)
                                     or re.search(r'\b(?:instructions?|recipient|fee\s+(?:wallet|address)|approv\w*|pre-?sign\w*|without\s+(?:a\s+|the\s+)?card)\b', m, re.I)):
        return INJECT_WARN
    return None


SCAM_WARN = ('⚠️ This looks like a scam. Real support never asks you to send coins, to "verify", "sync" or "unlock" a wallet, or to pay a '
             'fee to recover funds, and no one can double your coins. Kivy has no support staff who message you. Don\'t send anything — '
             'a transfer on Sui is final and can\'t be reversed.')
INJECT_WARN = ('I can\'t change how I work from the chat: there is no admin or developer mode, no check can be switched off, and nothing '
               'is ever sent without your own approval in your wallet. Tell me what you want to do (e.g. "swap 1 SUI to USDC") and '
               'I\'ll prepare it for you to check.')


AI_IDIOM_RE = re.compile(r"\blend\s+(?:me|us|him|her|them)\s+(?:your\s+|an?\s+)?(?:ears?|hand|minute|moment|sec|second)\b|\blend\s+a\s+hand\b"
                         r"|\bcould\s+i\s+owe\b|\bowe\s+(?:somebody|someone|anyone|money|more)\b|\blike\s+a\s+loan\b"
                         r"|\bwithdraw\s+(?:my|the|our|his|her|their)\s+(?:job\s+)?(?:application|complaint|offer|request|consent|support|candidacy|name|bid|resignation|membership|comment|statement|nomination|vote)\b"
                         r"|^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:write|compose|make\s+up|draft|improve|rewrite|suggest)\b.{0,60}?\b(?:song|poem|chorus|verse|rap|haiku|limerick|joke|punchline|story|lyrics?|tweet|slogan|heading|title|caption)\b"
                         r"|\b(?:this|it)\s+is\s+(?:just\s+)?(?:a\s+)?(?:song|poem|joke|story)\b|\bnot\s+a\s+wallet\s+request\b|\bclaim\s+to\s+fame\b|\bpunchline\b"
                         r"|\bdeep\s+(?:learning|dive|sleep|breath|thinking)\b|\bsuggest\s+a\s+(?:better\s+)?(?:heading|title|name)\b", re.I)   # a reviewer 1103: a song, a joke, a school heading


AFTER_TX_RE = re.compile(r"\b(?:receipt|preview\s+showed|pop-?up\s+closed|closed\s+itself|(?:touch(?:ing)?|press(?:ing)?|tap(?:ping)?|click(?:ing)?)\s+confirm\s+again"
                         r"|pay\s+twice|paid\s+twice|charged?\s+twice|double[-\s]?(?:charge|pay|spend)|hidden\s+(?:charge|fee))\b", re.I)
GAS_Q_RE = re.compile(r"\b(?:insufficient|not\s+enough|no)\s+gas\b|\bgas\b.{0,80}\b(?:fail(?:ed|s)?|why)\b|\bfail(?:ed|s)?\b.{0,80}\b(?:gas|sui\s+is\s+(?:slightly\s+)?lower)\b", re.I)
GAS_ANSWER = ('Every Sui transaction pays a small network fee (gas) in SUI — even getting your own coins back, because the network still does the work. '
              '"Insufficient gas" means the wallet has too little free SUI: add a little SUI (0.05 is plenty) and try again. A failed transaction '
              'still pays its gas, but nothing else in it happens — Kivy\'s fee is part of the same transaction, so it is only paid when it succeeds.')


PROTO_STATE_RE = re.compile(r"\b(?:paused|pauses?|frozen|freezes?|halted|halts?|hacked|exploit(?:ed)?|insolvent|bankrupt|shut\s+down|goes\s+down|went\s+down|is\s+down|rug(?:ged|\s+pull(?:ed)?)?)\b"
                            r"|\b(?:navi|suilend|scallop|ember|aftermath|cetus|bluefin|sui|kivy)\s+(?:is\s+)?(?:down|offline|broken)\b", re.I)


def ai_only(msg):
    """An idiom ("lend me your ears", "withdraw my application"), or a question asked in its own sentence after an order ("Lend
    0.24 SUI on NAVI. Will I still be able to withdraw while it's paused?"): the AI answers both parts — code builds nothing,
    not even a choice card (a reviewer 1101, 2026-11-01)."""
    m0 = str(msg or '')
    if AI_IDIOM_RE.search(m0) and not (SEND_VERB_RE.match(m0) and re.search(r'0x[0-9a-fA-F]{64}\b|\b[a-z0-9-]+\.sui\b|(?<![\w@])@[a-z0-9-]{2,}\b', m0, re.I)):
        return True                                   # "send 5 sui to bob.sui for a deep dive": an order with a recipient, not an idiom (review P-30)
    if '?' in m0 and AFTER_TX_RE.search(m0):          # "the preview showed more than the receipt — hidden charge?" (a reviewer 1103)
        return True
    if IF_Q_RE.search(m0) and G_VERB_RE.search(m0) and re.search(r'\d', m0) and not QUOTE_HINT_RE.search(m0):
        return True                                   # "if I lend 1 usdc on navi, can I withdraw it tomorrow?": an order inside a question (2026-11-10)
    if PROTO_STATE_RE.search(m0) and ('?' in m0 or QUESTION_RE.search(m0)) and not re.search(r'\d', m0):
        return True                                   # "if NAVI is paused can I still withdraw?": what a pause allows differs — the AI says so (2026-11-10)
    q = re.search(r'(?<=\S)\s+((?:Why|What|How|Will|Would|Can|Could|Is|Are|Does|Do|Should|When|Where)\b[^.!?]*\?)\s*$', m0)
    if q and G_VERB_RE.search(m0[:q.start()]):        # "…Amount: 0.43 Why does my receipt say withdraw?" (lines joined by tidy_wording)
        return True
    sents = [x for x in re.split(r'(?<=[.!?])\s+|\n+', m0) if x.strip()]
    if len(sents) < 2:
        return False
    return any(i > 0 and any(G_VERB_RE.search(y) for y in sents[:i]) and ((x.rstrip().endswith('?') and re.match(r'\s*(?:why|what|how|will|would|can|could|is|are|does|do|should|when|where)\b', x, re.I))
                          or re.match(r'\s*(?:(?:before|first)\b[^.]{0,40},\s*)?(?:please\s+)?(?:explain|tell\s+me\s+(?:why|what|how)|describe|summari[sz]e|rewrite|rephrase|translate)\b', x, re.I))
               for i, x in enumerate(sents))


def read_guard(msg, address):
    m0 = str(msg or '')
    sw = scam_warn(m0)
    if sw:
        return {'text': sw}
    low = m0.lower()
    money = bool(G_VERB_RE.search(m0)) or bool(re.match(r'^\s*(?:repay|pay\s+(?:back|off))\b', m0, re.I))
    # 0. an order QUOTED inside a story or a report ("Maya withdrew…, I'm telling you her story", "the scam DM reads '…'") is not an
    #    order for this wallet: a scam report gets the scam warning, a story goes to the AI (a reviewer 1031b, 2026-11-01)
    if '?' in m0 and GAS_Q_RE.search(m0) and not AFTER_TX_RE.search(m0):   # "wallet says insufficient gas on my withdrawal — why?" (a reviewer 1103)
        return {'text': GAS_ANSWER}
    if IF_Q_RE.search(m0):                          # "if I lend 1 usdc on navi, can I withdraw it tomorrow?": a question — answers and info
        return GUARD_AI                               # cards only, never a card to sign (2026-11-10)
    if STORY_RE.search(m0) and (money or re.search(r'\d', m0)):
        return {'text': SCAM_WARN} if re.search(r'\b(?:scam\w*|impersonat\w*|red\s+flags?|phish\w*|fake\s+support|recovery\s+wallet)\b', m0, re.I) else GUARD_AI
    if ai_only(m0):
        return GUARD_AI
    if money and AM.long_address(m0):                 # an address over 64 characters is never cut to its first 64 (R3, 2026-11-15)
        return {'text': AM.long_address(m0)}
    fix = AM.correction(m0) if money and not ADVICE_Q_RE.search(m0) else None
    if fix == AM.CHANGED:                             # "send 50 usdc, i mean 5 sui" built a send of BOTH: a changed order is never guessed
        return {'text': 'You changed the order partway through, so I won\'t guess which version you mean. Say the final order in one line '
                        '— e.g. "swap 0.5 SUI to USDC".'}
    if fix:                                           # "send 50, i mean 5 usdc": the corrected amount wins, said back (R2, 2026-11-15)
        return {'_rewrite': fix[0], '_say': fix[1]}
    if re.search(r'\d|\b(?:all|half|everything)\b', m0) and (money or re.search(r'\b(?:into|to|from|back|out\s+of)\b', m0, re.I)) and not ADVICE_Q_RE.search(m0):
        if CANCEL_RE.search(m0):
            return {'text': 'Okay — nothing happens. A card only does something when you approve it in your wallet.'}
        if CORRECT_RE.search(m0):
            return {'text': 'You changed the order partway through, so I won\'t guess which version you mean. Say the final order in one line '
                            '— e.g. "swap 0.5 SUI to USDC".'}
    if re.search(r"\b(?:stop|cancel|halt)\s+(?:anything|everything|whatever|all)\s+(?:that\s+)?(?:hasn'?t|has\s+not|isn'?t|is\s+not)\s+(?:gone\s+through|confirmed|signed|done|finished)", m0, re.I):
        return {'text': 'Nothing happens without your approval: a card you haven\'t approved in your wallet moves nothing — close it, or reject the '
                        'request in your wallet. A transaction already confirmed on Sui can\'t be undone; tell me if you want the opposite move.'}
    sp = re.match(r'^\s*(?:split|divide|share)\s+(\d+(?:\.\d+)?)\s+([A-Za-z][A-Za-z0-9]{1,9})\s+(?:evenly\s+|equally\s+)?(?:between|among|across)\s+(.+?)\s*[.!]*$', m0, re.I)
    if sp and _gcoin(sp.group(2)):                    # "split 0.3 sui evenly between 0x… and 0x…": a send of an equal share to each (2026-11-03)
        rx = r'0x[0-9a-fA-F]{64}\b|@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b'
        rc = re.findall(rx, sp.group(3), re.I)
        if len(rc) >= 2 and not re.sub(r'%s|,|\band\b|&|\s' % rx, '', sp.group(3), flags=re.I):
            each = _gnum(float(sp.group(1)) / len(rc))
            new = 'send %s %s each to %s and %s' % (each, _gcoin(sp.group(2)), ', '.join(rc[:-1]), rc[-1])
            return {'_rewrite': new, '_say': 'Read as %s %s each to %d recipients.' % (each, disp(_gcoin(sp.group(2))), len(rc))}
    fx = re.search(r'(?:[฿€£¥₩₹]\s*\d+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:baht|thb|yen|jpy|euros?|eur|gbp|quid|won|krw|rupees?|inr|pesos?|ringgit|myr|rmb|yuan|cny|sgd|aud|cad|hkd|chf|francs?)\b)\s*(?:worth|of)\b', m0, re.I)
    if fx and any(_gcoin(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', m0[fx.end():])):   # "฿12 of my SUI" was read as 12 SUI (a reviewer 1103)
        return {'text': 'Kivy reads amounts in coins or US dollars only, so I can\'t convert "%s". Say it in US dollars or in coins — e.g. "swap $1 of SUI to USDsui" '
                        'or "swap 0.5 SUI to USDsui".' % fx.group(0).replace(' worth', '').replace(' of', '').strip()}
    if not money:
        return None
    perp = bool(G_PERP_RE.search(m0))
    if re.search(r'\b(?:send|transfer|pay|give)\b', m0, re.I):   # the same address twice paid it once and dropped the rest (2026-11-03)
        rcs = [x.lower() for x in re.findall(r'0x[0-9a-fA-F]{64}\b|(?<![\w.])@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui\b', m0, re.I)]
        dup = next((x for x in rcs if rcs.count(x) > 1), None)
        if dup:
            short = (dup[:6] + '…' + dup[-4:]) if dup.startswith('0x') else dup
            return {'text': 'You named the same address twice (%s). Should it get both amounts? Say it as one send — e.g. "send 0.2 SUI to %s" — '
                            'or paste the other address.' % (short, short)}
    wsm = re.match(r'^\s*(?:withdraw|redeem|take\s+out)\s+(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b(.*?)\b(?:and|then|,)\s+(?:then\s+)?(send|transfer|lend|supply|deposit|swap|convert)\s+(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:it\s+|my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b', m0, re.I)
    if wsm and _gcoin(wsm.group(2)) and _gcoin(wsm.group(6)) == _gcoin(wsm.group(2)) and float(wsm.group(5)) > float(wsm.group(1)) + 1e-12 \
            and not re.search(r'\b(?:wallet|plus|add(?:ing)?|with)\b', wsm.group(3), re.I):   # "withdraw 1 … and send 2": the 2 was dropped silently
        c = disp(_gcoin(wsm.group(2)))
        return {'text': 'The withdrawal gives %s %s, but you asked to %s %s %s. Say one amount — e.g. "withdraw %s %s from … and %s it".'
                        % (wsm.group(1), c, wsm.group(4).lower(), wsm.group(5), c, wsm.group(5), c, wsm.group(4).lower())}
    xm = re.match(r"^\s*(lend|supply|deposit|swap|convert|send|transfer)\s+(?:all|everything)\s+(?:of\s+)?(?:my\s+|the\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:except|but|apart\s+from|other\s+than|minus|less|keeping|keep)\s+(\d+(?:\.\d+)?)(?:\s+([A-Za-z][A-Za-z0-9]{1,9}))?\b(.*)$", m0, re.I)
    if xm and xm.group(4) and not _gcoin(xm.group(4)):   # "…except 0.2 on navi": "on" is not the kept amount's coin
        xm = re.match(r"^\s*(lend|supply|deposit|swap|convert|send|transfer)\s+(?:all|everything)\s+(?:of\s+)?(?:my\s+|the\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:except|but|apart\s+from|other\s+than|minus|less|keeping|keep)\s+(\d+(?:\.\d+)?)()\b(.*)$", m0, re.I)
    xv = (xm.group(1), xm.group(2), xm.group(3), xm.group(5)) if xm and _gcoin(xm.group(2)) and (not xm.group(4) or _gcoin(xm.group(4)) == _gcoin(xm.group(2))) else None
    if not xv:                                        # the kept amount at the END ("swap all my sui to usdc but keep 0.1" built a 0.99 swap) or the coin after it (2026-11-03)
        x2 = re.match(r"^\s*(lend|supply|deposit|swap|convert|send|transfer)\s+(?:all|everything)\s+(?:of\s+)?(?:my\s+|the\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b(.*?)[\s,;]+(?:(?:but\s+|and\s+|while\s+)?(?:keep(?:ing)?|leav(?:e|ing))|but|except|minus|less)\s+(\d+(?:\.\d+)?)(?:\s+([A-Za-z][A-Za-z0-9]{1,9}))?(?:\s+(?:in\s+(?:my\s+|the\s+)?wallet|back|aside|for\s+gas))?\s*[.!]*$", m0, re.I)
        x3 = re.match(r"^\s*(lend|supply|deposit|swap|convert|send|transfer)\s+(?:all|everything)\s+(?:except|but|apart\s+from|other\s+than|minus)\s+(\d+(?:\.\d+)?)\s+([A-Za-z][A-Za-z0-9]{1,9})\b(.*)$", m0, re.I)
        if x2 and _gcoin(x2.group(2)) and (not x2.group(5) or _gcoin(x2.group(5)) == _gcoin(x2.group(2))):
            xv = (x2.group(1), x2.group(2), x2.group(4), x2.group(3))
        elif x3 and _gcoin(x3.group(3)):
            xv = (x3.group(1), x3.group(3), x3.group(2), x3.group(4))
    if xv and address:
        c = _gcoin(xv[1])                             # "lend all my sui except 0.2": the rest of the wallet balance (gas already kept)
        try:
            have = float(spendable(c, all_balances(address) or {}) or 0)
        except Exception:
            have = 0.0
        left = have - float(xv[2])
        if left <= 0:
            return {'text': 'You have %s %s to use, so nothing is left after keeping %s.' % (_gnum(have), disp(c), xv[2])}
        new = '%s %s %s%s' % (xv[0], _gnum(left), c, xv[3])
        return {'_rewrite': new, '_say': 'Read "all except %s" as %s %s.' % (xv[2], _gnum(left), disp(c))}
    jm = re.match(r"^\s*(?:(?:please|pls)\s+)?(?:don'?t|dont|do\s+not)\s+(%s)\b([^,;.]*)[,;.]\s*(?:just|only|instead|but)\s+((%s)\b.*)$" % (G_VERB, G_VERB), m0, re.I)
    if jm and jm.group(1).lower() != jm.group(4).lower():   # "don't lend my sui, just swap 0.3 of it to usdc": the second half is the order
        c = next((_gcoin(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', jm.group(2)) if _gcoin(w)), None)
        rest = re.sub(r'\b(\d+(?:\.\d+)?)\s+of\s+(?:it|them)\b', r'\1 ' + c, jm.group(3), flags=re.I) if c else jm.group(3)
        return {'_rewrite': rest, '_say': 'Read as: "%s".' % rest}
    wm = re.match(r"^\s*withdraw\s+(?:all\s+)?(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:and|&)\s+(?:all\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b(.*)$", m0, re.I)
    if wm and _gcoin(wm.group(1)) and _gcoin(wm.group(2)) and _gcoin(wm.group(1)) != _gcoin(wm.group(2)):
        tail = re.sub(r'\b(?:both|them|each)\b', 'it', wm.group(3), flags=re.I)   # "withdraw my ssui and usdsui then swap both to usdc"
        return {'text': 'One coin per withdrawal — which first?', 'widget': {'type': 'choose', 'options': [
            {'label': 'Withdraw my %s' % disp(_gcoin(x)), 'ask': ('withdraw my %s%s' % (_gcoin(x), tail)).strip()} for x in wm.group(1, 2)]}}
    lm = re.match(r"^\s*(?:go\s+)?(long|short)\s+([A-Za-z0-9&]{2,12})\s+(?:with|using)\s+(?:all\s+(?:of\s+)?|half\s+(?:of\s+)?)?my\s+(?:wallet\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$", m0, re.I)
    if lm and _gcoin(lm.group(3)) and _gcoin(lm.group(3)) != 'USDC':   # "long sui with all my wallet sui": margin is USDC from the perps account
        c = _gcoin(lm.group(3))
        return {'text': 'Perps margin is USDC in your perps account, not %s from the wallet. Swap it to USDC and deposit it first (one approval), then open the order:' % disp(c),
                'widget': {'type': 'choose', 'options': [{'label': 'Swap all my %s to USDC, deposit it' % disp(c), 'ask': 'swap all my %s to USDC and deposit it into perps' % c}]}}
    em = re.match(r"^\s*withdraw\s+(?:everything|all)\b.*?\b(?:except|but\s+(?:not|don'?t\s+touch|leave)|other\s+than|apart\s+from)\s+(?:from\s+)?(?:my\s+|the\s+)?(%s)\b" % G_LEND_MKT, m0, re.I)
    if em and address:                                # "withdraw everything but don't touch ember"
        try:
            rows = [r for r in lend_positions_rows(address) if em.group(1).lower().split()[0] not in str(r['name']).lower()]
        except Exception:
            rows = []
        if not rows:
            return {'text': 'You have nothing lent outside %s.' % em.group(1)}
        if len(rows) == 1:
            new = 'withdraw all %s from %s' % (rows[0]['coin'], rows[0]['name'])
            return {'_rewrite': new, '_say': 'Read as: "%s" (%s untouched).' % (new, em.group(1))}
        return {'text': 'One market per withdrawal — which first? (%s stays untouched.)' % em.group(1), 'widget': {'type': 'choose', 'options': [
            {'label': '%s on %s' % (disp(r['coin']), r['name']), 'ask': 'withdraw all %s from %s' % (r['coin'], r['name'])} for r in rows[:6]]}}
    spot = bool(G_SPOT_RE.search(m0))
    # 1. a question about a move, a past move, or "don't …": nothing to sign (the AI explains; its plan tools refuse these)
    if G_HYPO_RE.search(m0) or G_NEG_RE.search(m0) or INFO_Q_RE.search(m0) or ADVICE_Q_RE.search(m0):
        return GUARD_AI
    if re.search(r'(?<![\w.])\d+\.\d+\.\d+(?![\w.])', m0):   # "swap 1.5.2 sui" was read as 1.5 (2026-11-01)
        return {'text': 'I can\'t read "%s" as an amount. Type it as one number, e.g. "1.5".' % re.search(r'\d+\.\d+\.\d+', m0).group(0)}
    if re.search(r'\b(?:anything|any\s+coin|whatever)\s+(?:but|except|other\s+than|besides)\b', m0, re.I):
        return {'text': G_SAY_IT}
    um = re.search(r'\b(?:(?:to|into|for|buy|get)\s+|(?:lend|supply|deposit)\s+(?:\d+(?:\.\d+)?\s+)?(?:my\s+)?)(usdt|dai|busd|tusd|doge|shib|pepe|xrp|ada|bnb|matic|pol|avax|trx|ton|ltc|bch|dot|link|uni|arb|op)\b', m0, re.I)
    if um and not (ckey(um.group(1)) or lend_token(um.group(1))):
        return {'text': '%s isn\'t a coin Kivy supports. I work with SUI, USDC, DEEP, WAL, BTC, ETH, SOL and the coins Suilend or NAVI list.' % um.group(1).upper()}
    # 1b. the order changed partway ("…, actually use 0.27 SUI", "correction, 0.46") or was called off ("actually scrap the whole thing"):
    #     the readers take the FIRST amount and action, so never build either version (a reviewer 1031b, 2026-10-31)
    if CANCEL_RE.search(m0):
        return {'text': 'Okay — nothing happens. A card only does something when you approve it in your wallet.'}
    if CORRECT_RE.search(m0):
        return {'text': 'You changed the order partway through, so I won\'t guess which version you mean. Say the final order in one line '
                        '— e.g. "swap 0.5 SUI to USDC".'}
    # 1c. card / bank money, bridges, slippage or minimum terms, someone else's balance (2026-11-01 round)
    if re.search(r'\b(?:buy|purchase|get|pay\s+for)\b.{0,40}\b(?:with|using|by|via)\s+(?:my\s+|a\s+)?(?:credit\s+card|debit\s+card|card|bank(?:\s+(?:transfer|account))?|paypal|apple\s+pay|google\s+pay|cash|baht|thb|fiat|promptpay|visa|mastercard)\b', m0, re.I):
        return {'text': 'Kivy can\'t take card or bank payments — it only moves coins already in your Sui wallet. Buy SUI or USDC on an exchange '
                        '(or your wallet app\'s buy option), send it to this wallet, and I can swap or lend it from there.'}
    if re.search(r'\bbridg(?:e|ing)\b', m0, re.I) or re.match(r'^\s*(?:move|send|transfer)\b.*\b(?:to|into|onto)\s+(?:the\s+)?(?:ethereum|solana|arbitrum|polygon|avalanche|tron|optimism|bitcoin|bsc)\b(?!\s+(?:address|wallet))', m0, re.I):
        return {'text': 'Kivy only works on the Sui network, so it can\'t bridge or move coins to another chain. Use a bridge outside Kivy; '
                        'coins like ETH or SOL that live on Sui can be swapped here.'}
    if re.search(r'\bslippage\b', m0, re.I) and re.search(r'\b(?:swap|convert|exchange|trade|sell|buy)\b', m0, re.I) and re.search(r'\d', m0):
        base = re.sub(r'[\s,;]*(?:and\s+)?(?:with\s+|at\s+|set\s+)?(?:the\s+|my\s+|a\s+)?(?:max(?:imum)?\s+)?slippage\b.*$', '', m0, flags=re.I).strip()
        return {'text': 'Kivy sets the slippage itself and you can\'t change it from the chat: the card shows the minimum you\'ll receive, and the '
                        'chain refuses the swap if less would arrive.', 'widget': {'type': 'choose', 'options': [{'label': base, 'ask': base}]} if G_VERB_RE.search(base) and re.search(r'\d', base) else None}
    if re.search(r'\bmin(?:imum)?\s+(?:received|out|output|amount|return)\b.{0,15}?(?<![\d.])0(?:\.0+)?(?![\d.])|\bno\s+minimum\b', m0, re.I):
        return {'text': 'The minimum you receive can\'t be set to zero — it is what protects the swap from a bad price. The card shows it, and the '
                        'chain enforces it. Say the swap on its own, e.g. "swap 0.5 SUI to USDC".'}
    if re.search(r"\b(?:balance|coins?|funds|money|wallet)\s+(?:of|from|in)\s+(?:0x[0-9a-fA-F]{6,}|someone(?:\s+else)?|another\s+(?:person|user)|other\s+people|my\s+friend)\b|\bsomeone\s+else'?s\s+(?:balance|coins?|funds|money|wallet)\b", m0, re.I):
        return {'text': 'Kivy only uses the coins in your own connected wallet — never another wallet\'s balance.'}
    tt = re.match(r'^\s*(?:swap|convert|exchange|trade)\s+(\d+(?:\.\d+)?|all|half)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:to|into|for)\s+([A-Za-z][A-Za-z0-9]{1,9})\s*(?:,|and|&|\+)\s*([A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$', m0, re.I)
    if tt and _gcoin(tt.group(2)) and _gcoin(tt.group(3)) and _gcoin(tt.group(4)) and len({_gcoin(tt.group(i)) for i in (2, 3, 4)}) == 3:
        n, c, a, b = tt.group(1), _gcoin(tt.group(2)), _gcoin(tt.group(3)), _gcoin(tt.group(4))
        return {'text': 'Into both %s and %s — half each, in one approval?' % (disp(a), disp(b)), 'widget': {'type': 'choose', 'options': [
            {'label': 'Half to %s, half to %s' % (disp(a), disp(b)), 'ask': 'swap %s %s: half to %s, half to %s' % (n, c, a, b)},
            {'label': 'All to %s' % disp(a), 'ask': 'swap %s %s to %s' % (n, c, a)}, {'label': 'All to %s' % disp(b), 'ask': 'swap %s %s to %s' % (n, c, b)}]}}
    # 1d. two amounts of the SAME coin side by side ("swap 0.5 sui and 0.3 sui to usdc" built 0.5) and "twice" (built once): ask
    dm2 = re.search(r'(?<![\w.])(\d+(?:\.\d+)?)\s+([A-Za-z][A-Za-z0-9]{1,9})\s+(?:and|&|\+|plus)\s+(?:another\s+)?(\d+(?:\.\d+)?)\s+(?:more\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b', m0)
    if dm2 and _gcoin(dm2.group(2)) and _gcoin(dm2.group(2)) == _gcoin(dm2.group(4)):
        a, b, c = float(dm2.group(1)), float(dm2.group(3)), dm2.group(2)
        mk = lambda n: m0[:dm2.start()] + '%s %s' % (_gnum(n), c) + m0[dm2.end():]
        return {'text': 'You named two amounts of %s (%s and %s). One amount per card — which?' % (disp(_gcoin(c)), _gnum(a), _gnum(b)),
                'widget': {'type': 'choose', 'options': [{'label': 'Both: %s %s' % (_gnum(a + b), disp(_gcoin(c))), 'ask': mk(a + b)},
                                                         {'label': '%s %s' % (_gnum(a), disp(_gcoin(c))), 'ask': mk(a)}, {'label': '%s %s' % (_gnum(b), disp(_gcoin(c))), 'ask': mk(b)}]}}
    tw = re.search(r'\s*\b(twice|thrice|two\s+times|three\s+times|[23]\s+times)\b', m0, re.I)
    if tw and not G_PERP_RE.search(m0) and len(re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])', m0[:tw.start()] + m0[tw.end():])) == 1:
        k = 3 if re.match(r'\s*(?:thrice|three|3)', tw.group(0).strip(), re.I) else 2
        once = (m0[:tw.start()] + m0[tw.end():]).strip()
        nm = re.search(r'(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])', once)
        tot = once[:nm.start()] + _gnum(float(nm.group(1)) * k) + once[nm.end():]
        return {'text': 'Each card is one transaction. Do it once with the total, or just once?',
                'widget': {'type': 'choose', 'options': [{'label': tot, 'ask': tot}, {'label': once, 'ask': once}]}}
    two = AM.unsure(m0) or AM.two_sided(m0) or AM.conflict(m0) or AM.clause_conflict(m0) or AM.modified(m0)   # "send 5-10 usdc", "long btc $10 and $500": which one? never the first (R2,
                                                      # 2026-11-15); round 2: "1.000 sui" (1 or 1000?), two amounts in one clause of a multi-step request
    if two:
        return two
    if re.match(r'^\s*(?:send|transfer|pay|give)\b', m0, re.I) and re.search(r'\bto\s+(?:myself|me|my\s+own\s+(?:wallet|address|account)|this\s+(?:same\s+)?wallet)\s*[.!?]*$', m0, re.I):
        return {'text': 'That is your own wallet — sending to yourself moves nothing and only costs gas. To move coins to another account of yours, paste its address.'}
    if re.search(r'0x[0-9a-fA-F]{1,64}\s*::', m0) and re.search(r'\b(?:send|transfer|pay|give)\b', m0, re.I):   # P-33
        return {'text': 'That is a coin type or a program id (0x…::…), not a wallet address — coins sent there are lost for good. '
                        'Paste the receiver\'s wallet address (0x followed by 64 characters) or their name.sui.'}
    if re.match(r'^\s*(?:repay|pay\s+(?:back|off))\b', m0, re.I):   # P-32: there is no Kivy loan; it is not a send
        return {'text': 'Kivy never borrows, so there is no Kivy loan to repay. A loan you took on Suilend or NAVI is repaid in that app.'}
    # 2. a negative amount (P-27)
    if (re.search(r'(?<![\w.\-])-\s*\$?\s*\d', m0) or re.search(r'\b(?:minus|negative)\s+\$?\s*\d', m0, re.I)) and not re.search(r'(?:-|minus|negative)\s*\d+(?:\.\d+)?\s*x\b', m0, re.I):
        return {'text': 'An amount can\'t be negative. Say the amount as a plain number, e.g. "5".'}
    # 2b. another chain named: Kivy only works on Sui (a Sui swap was built for "swap 1 sui to usdc on ethereum mainnet", 2026-10-31)
    xc = re.search(r'\b(?:on|via|over|through|across)\s+(?:the\s+)?(ethereum|solana|bsc|bnb|binance\s+smart|base|arbitrum|polygon|avalanche|tron|optimism)\b'
                   r'|\b(ethereum|solana|bsc|bnb|binance|base|arbitrum|polygon|avalanche|tron|optimism|eth|sol)\s+(?:mainnet|chain|network|wallet|address)\b', m0, re.I)
    if xc and not perp:
        return {'text': 'Kivy only works on the Sui network, so it can\'t move coins on %s. Coins like ETH or SOL that live on Sui can be swapped here; '
                        'moving between networks needs a bridge outside Kivy.' % (xc.group(1) or xc.group(2)).strip().title()}
    lw = re.match(r'^\s*(?:swap|convert|exchange|trade|sell)\s+(\d+(?:\.\d+)?)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:in)?(?:to|for)\s+[A-Za-z][A-Za-z0-9]{1,9}\s*[.!]*$', m0, re.I)
    if lw and address and _gcoin(lw.group(2)):        # "swap 0.2 usdsui to sui" with the USDsui lent: say where it is (2026-11-03)
        try:
            held = float((all_balances(address) or {}).get(_gcoin(lw.group(2))) or 0)
        except Exception:
            held = None
        hint = lent_hint(address, _gcoin(lw.group(2))) if held is not None and held < float(lw.group(1)) else ''
        if hint:
            return {'text': 'Your wallet has %s %s, not enough for %s.%s' % (_gnum(held), disp(_gcoin(lw.group(2))), lw.group(1), hint)}
    sc = re.match(r'^\s*(?:swap|convert|exchange|trade|change|turn)\s+(?:\d+(?:\.\d+)?\s+|all\s+|half\s+)?(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:in)?(?:to|for)\s+([A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$', m0, re.I)
    if sc and _gcoin(sc.group(1)) and _gcoin(sc.group(1)) == _gcoin(sc.group(2)):
        return {'text': 'That is %s to %s — pick two different coins, e.g. "swap 1 SUI to USDC".' % (disp(_gcoin(sc.group(1))), disp(_gcoin(sc.group(1))))}
    # 2c. "withdraw 25% of my sSUI (as SUI)": a share of the ONE position holding that coin, said back as units (crashed on '25%', 2026-10-31)
    pw = re.match(r'^\s*(?:withdraw|redeem|take\s+out)\s+(\d+(?:\.\d+)?)\s*%\s+(?:of\s+)?(?:my\s+|the\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b(.*)$', m0, re.I)
    if pw and address and 0 < float(pw.group(1)) <= 100 and _gcoin(pw.group(2)):
        c = _gcoin(pw.group(2))
        try:
            pos = lend_positions(address) or {}
            rows = [{'coin': 'USDC', 'lent': float((pos.get(p) or {}).get('usdc') or 0), 'name': PROTOS[p]} for p in PROTOS]
            rows = [r for r in rows + list(other_lent(address) or []) if r.get('coin') == c and float(r.get('lent') or 0) > 0]
        except Exception:
            rows = []
        if len(rows) == 1:
            units = _gnum(float(rows[0]['lent']) * float(pw.group(1)) / 100)
            return {'_rewrite': 'withdraw %s %s%s' % (units, c, pw.group(3)),
                    '_say': 'Read %s%% of your %s as %s %s.' % (num_plain(float(pw.group(1))), disp(c), units, disp(c))}
    # 3. conditions / limit prices / terms on spot moves (P-17) — perps have real limit orders, so they keep theirs
    if not perp and not QUESTION_RE.search(m0):
        if (G_COND_RE.search(POLITE_RE.sub(' ', m0)) or AUTO_RE.search(POLITE_RE.sub(' ', m0))) and COND_Q_RE.search(m0):
            return GUARD_AI
        if G_COND_RE.search(POLITE_RE.sub(' ', m0)) or AUTO_RE.search(POLITE_RE.sub(' ', m0)) or (LATER_RE.search(m0) and not STORY_RE.search(m0) and not re.search(r'\b(?:need|want|use)\s+(?:it|them|this|that|the\s+money)\s+(?:by\s+|for\s+)?(?:tonight|tomorrow)', m0, re.I)):
            return {'text': "Kivy doesn't run automatic, repeating or if-this-then-that moves — every transaction waits for your own approval in "
                            "your wallet, and runs right then. Say the move you want now (e.g. \"swap 5 SUI to USDC\") and I'll prepare it."}
        if SWAP_VERB_RE.match(m0) and re.search(r'\bat\s+(?:a\s+price\s+of\s+|the\s+price\s+of\s+)?\$?\s*\d+(?:\.\d+)?(?![\d.]*\s*%)', m0, re.I):
            return {'text': 'A swap runs now, at the market price — Kivy can\'t hold a swap for a price. Say "swap 5 SUI to USDC" to swap now; '
                            'a limit order at a price is possible on perps ("long SUI $10 at 0.8").'}
        if re.search(r'\b(?:lend|supply|deposit|earn)\b', m0, re.I) and re.search(r'\bat\s+\d+(?:\.\d+)?\s*%|\bfor\s+(?:\d+|a|one|two|three|six)\s+(?:days?|weeks?|months?|years?)\b'
                                                                                 r'|\b(?:rate|apy|apr|yield)\s+(?:is\s+|goes\s+|stays\s+)?(?:above|over|below|under|at\s+least)\b', m0, re.I):
            return {'text': 'Lending has no fixed term and no rate you can set: Kivy lends now at the market\'s current rate, and you can withdraw any time. '
                            'Say "lend 5 USDC on NAVI" to lend now, or ask "best rate for USDC" to compare.'}
    # 13. perps margin is USDC only (P-16)
    dm = re.match(r'^\s*(?:deposit|add|fund|top\s*up|put|move)\s+(?:(\d+(?:\.\d+)?|all|half|max)\s+)?(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:straight\s+)?(?:to|into|in|on)\s+'
                  r'(?:my\s+)?(?:the\s+)?(?:aftermath\s+)?(?:perps?|margin|aftermath|collateral)\b', m0, re.I)
    if dm and _gcoin(dm.group(2)) and _gcoin(dm.group(2)) != 'USDC':
        c, n = _gcoin(dm.group(2)), dm.group(1) or 'all'
        if re.search(r"\b(?:don'?t|do\s+not|without)\s+swap", m0, re.I):
            return {'text': 'Perps margin is USDC only, so %s can\'t go in without a swap. Say "swap %s %s to USDC and deposit it into perps" to do both in one approval.' % (disp(c), n, c)}
        return {'text': 'Perps margin is USDC. Swap your %s to USDC and deposit it in one approval?' % disp(c),
                'widget': {'type': 'choose', 'options': [{'label': 'Swap %s %s to USDC, deposit it' % (n, disp(c)), 'ask': 'swap %s %s to USDC and deposit it into perps' % (n, c)}]}}
    # 4. except / not / without around a coin, market or recipient (P-5, P-12)
    if not (perp and not spot):
        rest = G_EXCEPT_OK.sub(' ', m0)
        if G_EXCEPT_RE.search(rest) and not G_KEEP_WD.search(m0):
            return {'text': G_SAY_IT}
    # 5. "$5 of BTC" / "20 dollars of SUI" = that many dollars OF the coin, at the live price (P-1, P-2, P-9)
    if not perp and (not re.match(r'^\s*(?:send|transfer|pay|give|withdraw|take|pull|redeem|buy|sell|ape)\b', m0, re.I)
                     or (AM.several(m0) and re.match(r'^\s*(?:send|transfer|pay|give|withdraw|take|pull|redeem)\b', m0, re.I))):
        # (round 2, 2026-11-16: also a multi-step or multi-send request that starts with send / withdraw — "send $5 of sui to
        #  bob.sui and $3 of usdc to alice.sui" sent 5 SUI; the one-step send and withdraw readers convert dollars themselves)
        um = next((x for x in G_USD_OF.finditer(m0) if _gcoin(x.group(5)) and (x.group(1) or x.group(3) or x.group(4))), None)
        if um:
            c = _gcoin(um.group(5))
            usd = float(um.group(1) or um.group(2))
            px = 1.0 if c in G_STABLE else float(coin_px(c) or 0)
            if not px:
                return {'text': "I can't get a live %s price right now to turn dollars into %s. Try again in a minute, or say the amount in %s." % (disp(c), disp(c), disp(c))}
            units = _gnum(usd / px)
            new = m0[:um.start()] + '%s %s' % (units, um.group(5)) + m0[um.end():]
            if c in G_STABLE:                          # "$1 of USDC" is 1 USDC: no "Read … (≈ $1 at $1)" line (2026-11-16: cut helper text)
                return {'_rewrite': new}
            return {'_rewrite': new, '_say': 'Read $%s of %s as %s %s (≈ $%s at $%s).' % (num_plain(usd), disp(c), units, disp(c), num_plain(usd), dec(px, 6))}
    tm = re.match(r'^\s*(?:swap|convert|trade|exchange)\s+(?:my\s+|some\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:to|for|into)\s+(\d+(?:\.\d+)?)\s*([A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$', m0, re.I)
    if tm and _gcoin(tm.group(1)) and _gcoin(tm.group(3)):   # "swap btc for 5 usdc": 5 is what they GET (P-1)
        a, b, n = _gcoin(tm.group(1)), _gcoin(tm.group(3)), float(tm.group(2))
        pa, pb = (1.0 if a in G_STABLE else float(coin_px(a) or 0)), (1.0 if b in G_STABLE else float(coin_px(b) or 0))
        if not (pa and pb) or a == b:
            return {'text': 'Say how much %s to spend, e.g. "swap 1 %s to %s".' % (disp(a), disp(a), disp(b))}
        units = _gnum(n * pb / pa)
        return {'text': 'A swap starts from what you spend. To get about %s %s you\'d spend about %s %s (the swap quote decides the exact amount):' % (num_plain(n), disp(b), units, disp(a)),
                'widget': {'type': 'choose', 'options': [{'label': 'Spend ≈ %s %s' % (units, disp(a)), 'sub': 'about %s %s back' % (num_plain(n), disp(b)),
                                                          'ask': 'swap %s %s to %s' % (units, a, b)}]}}
    # 6. "i have 20 sui, swap 5 to usdc": the number in "I have" is information, not the amount (P-3)
    hv = G_HAVE_RE.search(m0)
    if hv and not perp:
        rest = (m0[:hv.start()] + ' ' + m0[hv.end():]).strip(' ,;.')
        if G_VERB_RE.search(rest):
            hc = _gcoin(hv.group(2)) if hv.group(2) else None
            vm = re.search(r'\b(%s)\s+(\d+(?:\.\d+)?%%?|half|all|max|a\s+third|a\s+quarter)(?:\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9}))?' % G_VERB, rest, re.I)
            if vm and not (vm.group(3) and _gcoin(vm.group(3))):
                if not hc:
                    return {'text': 'Which coin and how much? Say it in one go, e.g. "swap 5 SUI to USDC".'}
                a0 = vm.group(2)
                put = ('%s %s' % (a0, hc)) if re.match(r'^\d+(?:\.\d+)?$', a0) else ('%s of my %s' % (a0, hc)) if a0.endswith('%') else ('%s %s' % (a0, hc)) if a0.lower() == 'max' else ('%s my %s' % (a0, hc))
                rest = rest[:vm.start(2)] + put + rest[vm.end(2):]
            if vm:
                return {'_rewrite': rest, '_say': 'Read as: "%s" (the %s you have is not the amount).' % (rest, hv.group(1))}
    # 7. two swaps in one sentence, or "and back" (P-7, P-31): each swap is its own approval
    if SWAP_VERB_RE.match(m0) and not re.search(r'\b(?:lend|send|withdraw|deposit|supply|keep)\b', m0, re.I):
        pairs = [p for p in G_PAIR_RE.finditer(m0) if _gcoin(p.group(2)) and _gcoin(p.group(3)) and _gcoin(p.group(2)) != _gcoin(p.group(3))]
        back = re.search(r'\b(?:and|then)\s+(?:swap\s+(?:it\s+|them\s+)?)?back\b|\b(?:and|then)\s+back\s+(?:to|into)\b', m0, re.I)
        src = {_gcoin(p.group(2)) for p in pairs}
        if len(pairs) >= 2 and len(src) == 1 and address and all(re.match(r'^\d+(?:\.\d+)?$', p.group(1)) for p in pairs):
            c = src.pop()
            try:
                have = float(spendable(c, all_balances(address) or {}) or 0)
            except Exception:
                have = None
            tot = sum(float(p.group(1)) for p in pairs)
            if have is not None and tot > have + 1e-9:   # "swap 0.6 sui to usdc and 0.6 sui to usdsui": each fits, both don't (2026-11-03)
                return {'text': 'Those swaps need %s %s in total, but your wallet has %s%s. Try smaller amounts.'
                                % (_gnum(tot), disp(c), _gnum(have), ' (a little SUI is kept back for gas)' if c == 'SUI' else '')}
        if len(pairs) >= 2 or (pairs and back):
            opts = [{'label': 'Swap %s %s → %s' % (p.group(1), disp(_gcoin(p.group(2))), disp(_gcoin(p.group(3)))),
                     'ask': 'swap %s %s to %s' % (p.group(1), _gcoin(p.group(2)), _gcoin(p.group(3)))} for p in pairs[:4]]
            return {'text': ('Swapping there and back only pays the swap fees twice. Here is the first swap — ask again for the way back if you still want it.'
                             if back and len(pairs) < 2 else 'Each of these swaps is its own approval. Which first?'),
                    'widget': {'type': 'choose', 'options': opts}}
    # 8. "swap 5 sui to usdc to 0x…": a recipient after a swap and no send word (P-14)
    rm = re.match(r'^\s*(?P<a>(?:swap|convert|trade|exchange)\b.*?\b(?:to|into|for)\s+[A-Za-z][A-Za-z0-9]{1,9})\s+(?:to|for)\s+(?P<r>0x[0-9a-fA-F]+|@?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.sui|@[a-z0-9-]{2,})\s*[.!]*$', m0, re.I)
    if rm and not re.search(r'\b(?:send|transfer|pay|give)\b', m0, re.I):
        return {'text': 'Should the swapped coins go to %s, or stay in your wallet?' % rm.group('r'),
                'widget': {'type': 'choose', 'options': [{'label': 'Swap, then send to %s' % (rm.group('r')[:10] + '…' if rm.group('r').startswith('0x') else rm.group('r')),
                                                          'ask': '%s and send it to %s' % (rm.group('a'), rm.group('r'))},
                                                         {'label': 'Just swap', 'sub': 'keep it in my wallet', 'ask': rm.group('a')}]}}
    # 9. "send 5 usdc from navi to 0x…" / "send my lent usdc": lent coins are withdrawn first, never taken from the wallet (P-15)
    if re.match(r'^\s*(?:send|transfer|pay|give)\b', m0, re.I):
        fm = re.search(r'\s+(?:from|out\s+of)\s+(?:my\s+)?(?:lent\s+)?(?P<mk>' + G_LEND_MKT + r')\b', m0, re.I)
        lent_w = re.search(r'\b(?:my\s+)?(?:lent|lending|supplied|deposited)\b', m0, re.I)
        if fm or lent_w:
            base = m0[:fm.start()] + m0[fm.end():] if fm else m0
            base = re.sub(r'\b(?:lent|lending|supplied|deposited)\s+', '', base, flags=re.I)
            sm = re.match(r'^\s*(?:send|transfer|pay|give)\s+(?P<w>.+?)\s+to\s+(?P<r>\S+)\s*[.!]*$', base, re.I)
            if sm and fm:
                return {'text': 'Lent coins are withdrawn first, then sent — one approval:',
                        'widget': {'type': 'choose', 'options': [{'label': 'Withdraw from %s and send' % fm.group('mk'), 'ask': 'withdraw %s from %s and send it to %s' % (sm.group('w'), fm.group('mk'), sm.group('r'))},
                                                                 {'label': 'Send from my wallet instead', 'ask': 'send %s to %s' % (sm.group('w'), sm.group('r'))}]}}
            if sm and address:
                coin = next((_gcoin(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', sm.group('w')) if _gcoin(w)), None)
                try:
                    rows = [r for r in lend_positions_rows(address) if coin and str(r['coin']).lower() == str(coin).lower()]
                except Exception:
                    rows = []
                if rows:
                    return {'text': 'Lent %s is withdrawn first, then sent — one approval. From which market?' % disp(coin),
                            'widget': {'type': 'choose', 'options': [{'label': '%s on %s' % (disp(r['coin']), r['name']), 'sub': '%s lent' % dec(r['lent']),
                                                                      'ask': 'withdraw %s from %s and send it to %s' % (sm.group('w'), mk_name(r['id']), sm.group('r'))} for r in rows[:6]]}}
                if coin:
                    return {'text': 'You have no %s lent. To send %s from your wallet, say "send %s to %s".' % (disp(coin), disp(coin), sm.group('w'), sm.group('r'))}
            return {'text': 'Lent coins have to be withdrawn first. Say which market, e.g. "withdraw 5 USDC from Suilend and send it to alice.sui".'}
    # 10. "swap 5 sui to usdc on navi": a lending market after a swap is not a swap venue — ask (P-19)
    if SWAP_VERB_RE.match(m0) and not re.search(r'\b(?:lend\w*|lent|withdraw|deposit|supply|keep\s+it|stay|without\s+changing)\b', m0, re.I):
        om = re.search(r'\b(?:to|into|for)\s+[A-Za-z][A-Za-z0-9]{1,9}\s+(?:on|at)\s+(?:my\s+)?(?P<mk>' + G_LEND_MKT + r')\b', m0, re.I)   # AFTER the target ("…in suilend to deep" swaps a lent coin in place)
        if om:
            p = next((p for p in G_PAIR_RE.finditer(m0) if _gcoin(p.group(2)) and _gcoin(p.group(3))), None)
            if p:
                a, b = _gcoin(p.group(2)), _gcoin(p.group(3))
                return {'text': '%s is a lending market, not a swap venue. Which did you mean?' % om.group('mk'),
                        'widget': {'type': 'choose', 'options': [{'label': 'Withdraw %s %s from %s and swap to %s' % (p.group(1), disp(a), om.group('mk'), disp(b)),
                                                                  'sub': 'the %s comes to your wallet' % disp(b), 'ask': 'withdraw %s %s from %s and swap to %s' % (p.group(1), a, om.group('mk'), b)},
                                                                 {'label': 'Swap %s %s → %s from my wallet' % (p.group(1), disp(a), disp(b)), 'ask': 'swap %s %s to %s' % (p.group(1), a, b)}]}}
            return {'text': '%s is a lending market, not a swap venue. Say "swap 5 SUI to USDC" for a swap from your wallet, or "withdraw 5 SUI from %s and swap to USDC".' % (om.group('mk'), om.group('mk'))}
    # 11. markets Kivy doesn't lend on, Aftermath as a "lending" place, "best rate" next to a named market (P-13)
    if re.search(r'\b(?:lend|supply|earn|deposit|put)\b', m0, re.I):
        bm = G_BAD_MKT.search(m0)
        if bm and not re.search(r'\b(?:swap|convert)\b[^,]*\b(?:on|via)\s+%s\b' % bm.group(1), m0, re.I):
            return {'text': 'Kivy doesn\'t lend on %s — it lends on Suilend (Main, Ember, Matrixdock) and NAVI (Main, Prime, High Yield). '
                            'Say e.g. "lend 5 USDC on NAVI", or "best rate for USDC" to compare.' % bm.group(1).title()}
        am = re.search(r'\b(?:lend|supply|earn)\b(?:\s+\w+){0,4}?\s+(?:on|in|at|into|to|with)\s+aftermath\b', m0, re.I)
        if am and not re.search(r'\b(?:perps?|margin)\b', m0, re.I):
            dep = m0[:am.start()] + re.sub(r'\s+(?:on|in|at|into|to|with)\s+aftermath\b', ' into perps', re.sub(r'\b(?:lend|supply|earn)\b', 'deposit', am.group(0), count=1, flags=re.I), flags=re.I) + m0[am.end():]
            lend = m0[:am.start()] + re.sub(r'\s+(?:on|in|at|into|to|with)\s+aftermath\b', ' on the best market', am.group(0), flags=re.I) + m0[am.end():]
            return {'text': 'Aftermath is where your perps margin sits — it isn\'t a lending market. Which did you mean?',
                    'widget': {'type': 'choose', 'options': [{'label': 'Deposit as perps margin', 'sub': 'USDC for trading, earns nothing', 'ask': dep},
                                                             {'label': 'Lend at the best rate', 'sub': 'Suilend or NAVI', 'ask': lend}]}}
        two = re.search(r'\s+(?:on|in|at|into)\s+(?P<m1>' + G_LEND_MKT + r')\s+(?:and|or|&|,)\s+(?:on\s+|in\s+)?(?P<m2>' + G_LEND_MKT + r')\b', m0, re.I)
        if two and re.match(r'^\s*(?:lend|supply|deposit|put|earn)\b', m0, re.I):   # "lend 5 usdc on navi and suilend": which? (P-12)
            base = (m0[:two.start()] + m0[two.end():]).strip()
            return {'text': 'One market per loan here — which one? (To split, say "lend half on %s and the rest on %s".)' % (two.group('m1'), two.group('m2')),
                    'widget': {'type': 'choose', 'options': [{'label': 'On %s' % x, 'ask': '%s on %s' % (base, x)} for x in (two.group('m1'), two.group('m2'))]}}
        bb = re.search(r'(?:\s*,\s*|\s+(?:at|on|with|for|in)\s+)(?:the\s+)?(?:best|highest|top)\s+(?:rate|yield|apy|apr|market)\b', m0, re.I)   # "NAVI best yield market" = NAVI's best: no ask
        nm = re.search(r'\s+(?:on|in|at|into)\s+(?:the\s+)?(?P<mk>' + G_LEND_MKT + r')\b', m0, re.I)
        if bb and nm:
            only_mk = (m0[:bb.start()] + m0[bb.end():]).strip()
            only_best = (m0[:nm.start()] + m0[nm.end():]).strip()
            return {'text': 'On %s, or wherever the rate is best?' % nm.group('mk'),
                    'widget': {'type': 'choose', 'options': [{'label': 'On %s' % nm.group('mk'), 'ask': only_mk}, {'label': 'The best rate anywhere', 'ask': only_best}]}}
    # 12. "lend half my sui and swap the rest" / "swap half … and lend the other half": the part kept as the SAME coin can't
    #     ride in a chain that swaps everything after its start (P-4) — ask for it one move at a time
    if (re.match(r'^\s*(?:lend|send|deposit|supply|keep)\b', m0, re.I) and re.search(r'\b(?:swap|convert)\s+(?:the\s+)?(?:rest|remaining|remainder|other\s+half|leftover)\b', m0, re.I)) \
            or re.search(r'\b(?:swap|convert)\s+(?:the\s+)?(?:half\b|\d+(?:\.\d+)?\s*%|a\s+third\b|a\s+quarter\b).*\b(?:lend|send|deposit|supply|keep)\s+(?:the\s+)?other\s+half\b', m0, re.I):   # only when the SWAP takes a part (fix 2)
        c = next((_gcoin(w) for w in re.findall(r'[A-Za-z][A-Za-z0-9]{1,9}', m0) if _gcoin(w)), None)
        cn = disp(c) if c else 'the coin'
        return {'text': 'Keeping part as %s and swapping the rest are two separate approvals, so I won\'t join them in one card. '
                        'Say them one at a time — e.g. "lend half my %s", then "swap the rest of my %s to USDC".' % (cn, cn, cn)}
    # 14. perps: an open order with a stop-loss / take-profit or a close, two orders, "with N usdc" (P-20, P-21)
    if re.search(r'\b(?:long|short)\b', m0, re.I):
        sides = re.findall(r'\b(?:long|short)\b', m0, re.I)
        sized = re.search(r'\$\s*\d|\d+(?:\.\d+)?\s*(?:usd|usdc|dollars?)\b|\d+(?:\.\d+)?\s+[A-Za-z]{2,}', m0, re.I)
        opn = re.match(r'^\s*(?:go\s+|open\s+(?:a\s+)?)?(?:long|short)\b', m0, re.I)
        if opn and sized:
            ex = re.search(r'\s*(?:,|\band\b|\bwith\b|\bthen\b|\+|;)*\s*(?:a\s+|an\s+)?(?:(?:stop[\s-]*loss|take[\s-]*profit|\bsl\b|\btp\b|stop\b|trailing\s+stop)\b.*|close\b.*)$', m0, re.I)
            if ex:
                first = m0[:ex.start()].strip(' ,')
                what = 'closing it' if re.search(r'\bclose\b', ex.group(0), re.I) else 'a stop-loss / take-profit'
                esym = next((chart_symbol(x) for x in re.findall(r'[A-Za-z][A-Za-z0-9&]{1,11}', first)
                             if x.lower() not in ('long', 'short', 'go', 'open', 'usd', 'usdc', 'dollars', 'margin', 'with') and chart_symbol(x)), None)
                eg = 'stop loss at <price> on my %s %s' % (esym or '<market>', 'short' if re.search(r'\bshort\b', first, re.I) else 'long')
                return {'text': ('Trailing stops aren\'t offered. ' if re.search(r'\btrailing\b', ex.group(0), re.I) else '')
                                + 'I\'ll open the order on its own; %s is a separate step on the open position (e.g. "%s").' % (what, eg),
                        'widget': {'type': 'choose', 'options': [{'label': 'Open: %s' % first, 'ask': first}]}}
        if len(sides) >= 2 and opn:
            parts = [x.strip(' ,') for x in re.split(r'\s*(?:,|;|\band\b|\bthen\b)\s*(?=(?:go\s+|open\s+(?:a\s+)?)?(?:long|short)\b)', m0, flags=re.I) if x.strip(' ,')]
            return {'text': 'One order at a time — which first?', 'widget': {'type': 'choose', 'options': [{'label': x, 'ask': x} for x in parts[:4]]}}
        two = re.match(r'^\s*(?:go\s+)?(long|short)\s+([A-Za-z0-9&]{2,12})\s+(?:and|&|,)\s+([A-Za-z0-9&]{2,12})\b(.*)$', m0, re.I)
        if two and chart_symbol(two.group(2)) and chart_symbol(two.group(3)):
            return {'text': 'One order at a time — which first?', 'widget': {'type': 'choose', 'options': [
                {'label': '%s %s%s' % (two.group(1).title(), chart_symbol(x), two.group(4)), 'ask': '%s %s%s' % (two.group(1), chart_symbol(x), two.group(4))} for x in (two.group(2), two.group(3))]}}
        wu = re.search(r'\b(?:with|using)\s+\$?\s*(\d+(?:\.\d+)?)\s*(?:usdc|usd|dollars?)\b(?!\s*(?:of\s+)?(?:margin|collateral))', m0, re.I)
        if opn and wu and not re.search(r'\d\s*x\b|\bmargin\b|\bcollateral\b', m0, re.I):
            om2 = re.match(r'^\s*(?:go\s+|open\s+(?:a\s+)?)?(long|short)\s+([A-Za-z0-9&]{2,12})\b', m0, re.I)
            sym = chart_symbol(om2.group(2)) if om2 else None
            if sym:
                n, sd = wu.group(1), om2.group(1).lower()
                return {'text': 'Is %s the order size, or the USDC margin you put in?' % n, 'widget': {'type': 'choose', 'options': [
                    {'label': '$%s order' % n, 'sub': '%s %s, size $%s' % (sd, sym, n), 'ask': '%s %s $%s' % (sd, sym, n)},
                    {'label': '%s USDC margin' % n, 'sub': 'the money you put in', 'ask': '%s %s with %s usdc margin' % (sd, sym, n)}]}}
    # 15. "sell my sui" / "buy sui with usdc" / bare "sell sui": the spot picker the right way round (P-10)
    sm2 = re.match(r'^\s*sell\s+(all\s+(?:of\s+)?my\s+|my\s+|some\s+(?:of\s+)?my\s+)([A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$', m0, re.I)
    if sm2 and _gcoin(sm2.group(2)):
        c = _gcoin(sm2.group(2))
        return {'_rewrite': '%sswap %s%s to %s' % ('', 'all my ' if sm2.group(1).lower().startswith('all') else '', c, 'SUI' if c == 'USDC' else 'USDC')}
    bw = re.match(r'^\s*buy\s+(?:some\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:with|using)\s+(?:(\d+(?:\.\d+)?|all|half)\s+(?:of\s+)?)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$', m0, re.I)
    if bw and _gcoin(bw.group(1)) and _gcoin(bw.group(3)):
        return {'_rewrite': 'swap %s%s to %s' % ((bw.group(2) + ' ') if bw.group(2) else '', _gcoin(bw.group(3)), _gcoin(bw.group(1)))}
    sb = re.match(r'^\s*sell\s+([A-Za-z][A-Za-z0-9]{1,9})\s*[.!]*$', m0, re.I)
    if sb and _gcoin(sb.group(1)) and _gcoin(sb.group(1)) != 'USDC':
        c = _gcoin(sb.group(1))
        opts = [{'label': 'Spot — sell %s for USDC' % disp(c), 'sub': 'swap the %s in your wallet' % disp(c), 'ask': 'swap %s to USDC' % c}]
        if chart_symbol(c):
            opts.append({'label': 'Perps — short %s' % chart_symbol(c), 'sub': 'a leveraged bet it falls', 'ask': 'short %s' % chart_symbol(c)})
        return {'text': 'Spot or perps?', 'widget': {'type': 'choose', 'options': opts}}
    # 16. an amount below the coin's smallest unit (P-29)
    for nm2 in re.finditer(r'(?<![\w.])(\d+\.\d+)\s+(?:of\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9]{1,9})\b', m0):
        c = _gcoin(nm2.group(2))
        try:
            d = int(cinfo(c)['decimals']) if c else None
        except Exception:
            d = None
        if c and re.search(r'\b(?:swap|convert|exchange|lend|supply|deposit)\b', m0, re.I) and 0 < float(nm2.group(1)) * float(coin_px(c) or 0) < 0.0001:
            return {'text': '%s %s is worth less than $0.0001 — too small to swap or lend: the network fee alone is more. Try a bigger amount.' % (nm2.group(1), disp(c))}
        if c and re.search(r'\b(?:withdraw|redeem|take\s+out)\b', m0, re.I) and 0 < float(nm2.group(1)) * float(coin_px(c) or 0) < 0.0001 and float(nm2.group(1)) >= 10 ** -(d or 0):
            return {'text': '%s %s is worth less than $0.0001 — too small to withdraw: the network fee alone is more. Try a bigger amount.' % (nm2.group(1), disp(c))}
        if d is not None and 0 < float(nm2.group(1)) < 10 ** -d:
            return {'text': '%s %s is smaller than the smallest %s amount (%s). Try a bigger amount.' % (nm2.group(1), disp(c), disp(c), _gnum(10 ** -d))}
    return None


def withdraw_coverage(msg, r, address=None):
    """A withdraw card that keeps the coin while the sentence names another coin to end in ("… and make it vSUI"): never shown —
    the swap would be dropped without a word (a reviewer 1031b, 2026-11-01). Ask for the plain wording instead."""
    w = (r or {}).get('widget') if isinstance(r, dict) else None
    if not w or w.get('type') != 'withdraw' or str(w.get('to') or '').lower() != str(w.get('coin') or '').lower():
        return r
    for x in re.findall(r'\b(?:into|to|as|make\s+(?:it|that|them)|for)\s+([A-Za-z][A-Za-z0-9]{1,9})\b', str(msg or ''), re.I):
        c = None if x.lower() in G_STOP else (ckey(x) or lend_token(x))
        if c and c.lower() != str(w.get('coin')).lower():
            ask = 'withdraw %s %s from %s, swap it to %s, keep it in my wallet' % (_gnum(w['preset']) if w.get('preset') else 'all', w['coin'], w.get('name') or '', c)
            try:                                      # the same move as one chain (the withdraw card can't end in every coin)
                ch = chain_parse(ask, address) if address and w.get('name') else None
            except Exception:
                ch = None
            if ch and (ch.get('widget') or {}).get('type') == 'chain':
                ch['text'] = 'Read as: "%s". %s' % (ask, ch.get('text') or '')
                return ch
            return {'text': 'I didn\'t catch the swap to %s, so I won\'t show a card without it. Say it like this: "withdraw %s%s from %s, then convert to %s".'
                            % (disp(c), ('%s ' % _gnum(w['preset'])) if w.get('preset') else '', disp(w['coin']), w.get('name') or 'the market', disp(c))}
    return r


def reread_allowed(msg, address):
    """May a re-reading of this message (the route check's "read as") become a card? No when the read guard itself refused the
    message or sent it to the AI — a question, a "don't", a story, a condition, an except — or when it holds a negated or
    hypothetical order anywhere ("my friend says never send 5 usdc to X", "i was told not to swap 100 usdc"). The model that
    proposes the re-reading never decides this (review scan R4, 2026-11-15)."""
    m = str(msg or '')
    if G_HYPO_RE.search(m) or G_NEG_RE.search(m) or G_NEG_ANY_RE.search(m) or ai_only(m) or STORY_RE.search(m) \
            or re.search(r"\b(?:not|never)\s+(?:to\s+|ever\s+|going\s+to\s+|gonna\s+)*%s\b" % G_VERB, m, re.I):
        return False
    try:
        g = read_guard(m, address)
    except Exception:
        return False
    return g is None or (isinstance(g, dict) and '_rewrite' in g)


def guarded(fn, msg, address, *rest):
    """Run read_guard before a reader entry point: a reply is returned, a rewrite is read by the same entry point (its "Read …"
    line in front), GUARD_AI gives None (the AI answers). Depth-capped so a rewrite can never loop."""
    if getattr(XW, 'guard_depth', 0) >= 3:
        return 'go'
    try:
        g = read_guard(msg, address)
    except Exception as exc:
        print('kivy read_guard error: %s' % str(exc)[:160], flush=True)
        g = None
    if g is None:
        return 'go'
    if g is GUARD_AI:
        return None
    if '_rewrite' in g:
        XW.guard_depth = getattr(XW, 'guard_depth', 0) + 1
        try:
            r = fn(g['_rewrite'], address, *rest)
        finally:
            XW.guard_depth -= 1
        if r and g.get('_say'):
            r = dict(r, text=g['_say'] + ' ' + (r.get('text') or ''))
        return r
    return g


def guard_info(fn, msg, address, *rest):
    """For a question / "don't" sentence (GUARD_AI): the reader's answer only if it is text or an info card — never a card to sign."""
    if STORY_RE.search(str(msg or '')) or ai_only(msg):   # a story or a report quoting an order, or a question after an order: the AI talks about it
        return None
    d0, XW.guard_depth = getattr(XW, 'guard_depth', 0), 3
    try:
        t = fn(msg, address, *rest)
    except Exception:
        t = None
    finally:
        XW.guard_depth = d0
    w = (t or {}).get('widget') or {}
    return t if t and not t.get('plan') and (not w or w.get('type') in G_INFO_CARDS) else None


# ── Names from the other reader modules (server split 2). The readers call each other in a cycle, so these imports come
#    last: by the time any module reads this one, all of this module's own code is defined. Each name comes from the module
#    that defines it and is held here, in this module's namespace, so the tests' S.name = fake reaches it (kivy.seam). ──
from .perps import chart_symbol, num_plain  # noqa: E402
from .situations import AUTO_RE, LATER_RE, QUOTE_HINT_RE, mk_name  # noqa: E402
from .chainreads import all_balances, cinfo, ckey, dec  # noqa: E402
from .lending import (  # noqa: E402
    PROTOS, coin_px, lend_positions, lend_positions_rows, lend_token, lent_hint, other_lent, spendable)
from .chains import chain_parse  # noqa: E402
from .sends import SEND_VERB_RE, XW  # noqa: E402
