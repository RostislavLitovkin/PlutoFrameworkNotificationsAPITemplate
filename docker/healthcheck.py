"""
Container health probe.

Answers one question: is the WSGI server accepting connections and speaking HTTP?

An HTTP error status counts as healthy on purpose. The probe connects over
127.0.0.1, so Django compares that Host against ALLOWED_HOSTS and may answer 400 —
a configuration answer from a live process, not a dead one. Only a refused
connection, a timeout, or a malformed reply means the container should be replaced.
Database reachability is deliberately out of scope: a healthy app with a briefly
unreachable database should not be restarted in a loop.
"""
import os
import sys
import urllib.error
import urllib.request

URL = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/admin/login/"

try:
    urllib.request.urlopen(URL, timeout=4)
except urllib.error.HTTPError:
    pass
except Exception as error:  # connection refused, timeout, bad response
    print(f"unhealthy: {error}", file=sys.stderr)
    sys.exit(1)

sys.exit(0)
