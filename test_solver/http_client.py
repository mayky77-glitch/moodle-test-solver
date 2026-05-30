from __future__ import annotations

import ssl
from urllib.request import Request, urlopen

import certifi


def open_url(request: Request | str, timeout: float):
    context = ssl.create_default_context(cafile=certifi.where())
    return urlopen(request, timeout=timeout, context=context)
