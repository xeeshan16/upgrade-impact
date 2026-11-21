import json

import requests  # not the package under test


def unrelated() -> str:
    return json.dumps({"ok": requests.__name__})
