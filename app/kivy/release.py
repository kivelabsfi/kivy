"""The release id check across Kivy's one network boundary, site -> builder (architecture programme, 2026-11-14).

Both services read the id of the release they run from (settings.RELEASE_ID; the builder serves its own on /health).
A site talking to a builder from another release is how a stale builder once built a different transaction than the
page showed (2026-10-20). A mismatch is logged ('kivy release: mismatch site=<id> builder=<id>') and, since phase 1, every
BUILD is refused while it lasts (build_allowed(), called by kivy/upstream.py): the user sees "Kivy is updating" for the few
seconds between the builder's and the site's restart in a promote. Reads still pass. Outside a release ('none') nothing is
enforced (tests, hand runs).
"""
# How it runs: imported by site/server.py; watch() starts one daemon thread in the kivy-site service.
import json
import threading
import time
import urllib.request

from . import settings

state = {'builder': None, 'checked': 0.0}


def builder_release(fresh=False):
    """The builder's release id (cached 60 s); None when the builder does not answer."""
    if fresh or time.time() - state['checked'] > 60:
        try:
            d = json.load(urllib.request.urlopen(settings.BUILDER + '/health', timeout=3))
            state['builder'] = str(d.get('release') or 'none')
        except Exception:
            state['builder'] = None
        state['checked'] = time.time()
    return state['builder']


def watch(log=print):
    def loop():
        last = None
        while True:
            b = builder_release(fresh=True)
            if b is not None and b != settings.RELEASE_ID and b != last:
                log('kivy release: mismatch site=%s builder=%s (%s)' % (settings.RELEASE_ID, b, settings.STAGE), flush=True)
            last = b
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name='release-watch').start()


def health():
    out = {'ok': True, 'service': 'kivy-site', 'stage': settings.STAGE, 'release': settings.RELEASE_ID,
           'builder_release': state['builder']}
    try:                                             # can the AI answer? (a key was lost for ~8 h at the first cutover, 2026-11-14)
        from . import limits                         # server.py hands the brain to kivy.limits at start-up
        out['ai'] = bool(limits.BRAIN and any(limits.BRAIN.keys_found().values()))
    except Exception:
        out['ai'] = False
    return out


UPDATING = "Kivy is updating right now — try again in a minute. Nothing was prepared."


def build_allowed():
    """True unless both sides run releases and they differ (checked again at once before refusing, cached 60 s otherwise)."""
    if settings.RELEASE_ID == 'none':
        return True
    b = builder_release()
    if b is None or b == 'none' or b == settings.RELEASE_ID:
        return True
    b = builder_release(fresh=True)                  # the builder may have just come up on our release
    if b is None or b == 'none' or b == settings.RELEASE_ID:
        return True
    print('kivy release: build refused — site %s, builder %s' % (settings.RELEASE_ID, b), flush=True)
    return False
