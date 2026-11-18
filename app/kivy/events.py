"""The registry of Kivy's log-line kinds (architecture programme, review §10, 2026-11-14). The watchdog (tools/watchdog.py) reads
the services' journals and alerts on lines that START with one of these prefixes; the services print them. The two used to agree
by text alone — reword a print and an alert went quiet. tests/test_events_20261114.py checks that every prefix the watchdog
matches is registered here and that every registered prefix is still printed by the code.

Add a kind here first when a new line should alert; never reword a registered prefix without changing the watchdog with it.

Request ids (kivy/reqid.py, 2026-11-14): a line printed while a request is answered ENDS with " [req <id>]", in the server and in
the builder alike. Never put anything at the start of a line: the watchdog reads the start.
"""
# How it runs: read by tests/test_events_20261114.py (and by people); the services do not import it.

KINDS = {
    # prefix: (who prints it, what it means — the watchdog's reaction)
    'kivy busy:': ('site', 'a slot pool was full and a request was refused; 3 in 5 min alert'),
    'kivy limit:': ('site', 'an abuse limit stopped an address; 3 in 5 min alert'),
    'kivy slow body:': ('site', 'a POST body arrived too slowly and was cut; 3 in 5 min alert'),
    'kivy check dropped:': ('site', 'check 2 queue full, a check waits (never lost); alert'),
    'kivy check narrowed:': ('site', 'check 2 could not check one part (e.g. lent amount); alert'),
    'kivy refused build:': ('site', 'a built transaction broke the allowlist or call rules, refused before its card; alert'),
    'kivy pkg log not written:': ('site', 'the unknown-program log could not be written (the refusal stood); alert'),
    'kivy handler error:': ('site', 'an unexpected error in a request handler; alert'),
    'kivy brain:': ('site', 'one AI answer: model, tokens, cost (counted, not alerted)'),
    'kivy perf:': ('site', 'the 5-minute timing summary per route (slow alerts)'),
    'kivy perps refused:': ('builder', 'an Aftermath perps answer did not match the request, refused; alert + fixer'),
    'kivy release:': ('site', 'site and builder run different releases (builds refused while it lasts); alert'),
    'kivy ledger:': ('site', 'the ledger dual-write compare; a non-zero difference alerts'),
    'kivy plan shadow:': ('site', 'plan-first shadow: the request-only plan differs from today\'s (counted, read weekly)'),
    'kivy limits:': ('site', 'the limits store (limits.db): on at start, or off for a minute after a failure — in-memory limits meanwhile ("store" lines alert, once an hour at most)'),
    'kivy refused build (builder):': ('builder', 'a build the BUILDER refused by Kivy\'s own rules (allowlist, call rules, object rule) — never one the '
                                      'user caused; alert, once an hour per route with the latest reason (fix package 6, OPS-A1)'),
    'kivy-lend build:': ('builder', 'one finished build: route, ok or refused (reason), time, request id — the request trail (not alerted; '
                         'until no release before 2026-11-17 can be rolled back to, the watchdog also reads a rule refusal from it)'),
}
