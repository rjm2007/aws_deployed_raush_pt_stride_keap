from __future__ import annotations

import time
import urllib.error
import urllib.request

URLS = ("http://localhost:8000/ready", "http://localhost:9000/health")

for _ in range(30):
    try:
        if all(urllib.request.urlopen(url, timeout=2).status == 200 for url in URLS):
            break
    except (urllib.error.URLError, TimeoutError):
        pass
    time.sleep(1)
else:
    raise SystemExit("Local services did not become healthy. Run docker compose logs.")
