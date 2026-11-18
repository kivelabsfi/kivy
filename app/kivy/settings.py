"""Where Kivy's server finds things: paths, addresses and the release id, in one place (architecture programme phase 0,
2026-11-14). Every value comes from the environment, with today's live values as defaults, so a staging twin can run
the same code on other ports with its own data folder.

  KIVY_DATA         the service's one writable folder (default: data/ next to server.py; inside a release /opt/kivy/data, or
                    a temp folder for a root process — never a folder inside the release)
  KIVY_PORT         the site's port (8772)
  KIVY_BUILDER_URL  the builder, kivy-lend (http://127.0.0.1:8799)
  KIVY_GATEWAY_URL  the chain gateway, kivy-chain (http://127.0.0.1:8798)
  KIVY_PAUSE_FILE   the pause switch (default: PAUSED next to the data folder = /opt/kivy/PAUSED; root-only)
  KIVY_DIARY        Kivy's own diary, which also holds transaction labels (/srv/kivy/diary.md)
  KIVY_HOME         where brain.py and knowledge/ are (default: the folder above site/ when it holds brain.py;
                    ignored inside a release, which always uses its own)
  KIVY_STAGE        'live' or 'staging' (shown on /health and in log lines)
The release id comes from the RELEASE file a release build writes (tools/release.py); 'none' when there is none.
"""
# How it runs: imported by site/server.py, accounts.py, metrics.py and shadow.py inside kivy-site; nothing runs on its own.
import json
import os
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent          # site folder: server.py, the page, static/
ROOT = SITE.parent                                     # a release or the repo: site/ brain.py knowledge/ builder/
IN_RELEASE = (ROOT / 'RELEASE').exists()               # a release always uses its own brain.py and knowledge/
HOME = (ROOT if IN_RELEASE else Path(os.environ.get('KIVY_HOME') or (ROOT if (ROOT / 'brain.py').exists() else '/srv/kivy'))).resolve()
# inside a release the code's own folder holds no data: anything importing it (tools, the Walrus exporter, tests) gets the live
# data folder unless KIVY_DATA says otherwise (the Walrus export broke on this for 7 minutes after the 2026-11-14 cutover)
# ... but never for a ROOT process without KIVY_DATA (tests, tools run by hand): a root-owned -wal/-shm file once locked the
# service out of its own database (2026-11-11). Services set KIVY_DATA; the Walrus saver runs as kivy.
# ... and never a folder INSIDE the release either: the gate's test run wrote intents.db, metrics.db, tokens … into every release
# it tested (review scan 2026-11-15 A1; releases are never edited). Such a root process gets its own temp folder, removed at exit
# (tests/run_noai.sh sets one KIVY_DATA for a whole run, so its processes share it).
def _data():
    if os.environ.get('KIVY_DATA'):
        return Path(os.environ['KIVY_DATA'])
    if not (ROOT / 'RELEASE').exists():
        return SITE / 'data'
    if os.geteuid() != 0:
        return Path('/opt/kivy/data')
    import atexit
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix='kivy-data-')
    atexit.register(shutil.rmtree, tmp, True)
    return Path(tmp)


DATA = _data()
PORT = int(os.environ.get('KIVY_PORT') or 8772)
BUILDER = (os.environ.get('KIVY_BUILDER_URL') or 'http://127.0.0.1:8799').rstrip('/')
GATEWAY = (os.environ.get('KIVY_GATEWAY_URL') or 'http://127.0.0.1:8798').rstrip('/')
PAUSE_FILE = Path(os.environ.get('KIVY_PAUSE_FILE') or DATA.parent / 'PAUSED')
DIARY = Path(os.environ.get('KIVY_DIARY') or '/srv/kivy/diary.md')
STAGE = os.environ.get('KIVY_STAGE') or 'live'


def key_file(name):
    """A provider key file (tatum.key …): from systemd's credentials folder when the unit hands it in (LoadCredential — the
    keys live in the root-only /opt/kivy/keys since 2026-11-14), else the data folder as before."""
    cred = os.environ.get('CREDENTIALS_DIRECTORY')
    if cred and (Path(cred) / name).exists():
        return Path(cred) / name
    return DATA / name


def _release():
    for f in (ROOT / 'RELEASE', SITE / 'RELEASE'):
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {'id': 'none'}


RELEASE = _release()
RELEASE_ID = str(RELEASE.get('id') or 'none')
