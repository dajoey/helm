"""What Sentry/the error tracker events never carry: the web role's press key (HELM_WEB_PRESS_KEY) and its
header, and the deployment bearer (HELM_APP_TOKEN), either of which a frame's local variables can
hold.

The SDK's default denylist covers Authorization, cookies and the usual secret names, not our own
header, and it does not look inside a dict nested in a frame's local variables. So two layers: the
SDK's `EventScrubber` with our names added, and `before_send`, which walks the whole event and
replaces each secret's own value wherever it appears (a header, a frame variable, a message).
"""
from __future__ import annotations

import os

EXTRA_DENY = ["x-helm-press-key", "x_helm_press_key", "helm_web_press_key", "press_key"]
FILTERED = "[Filtered]"


def event_scrubber():
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
    return EventScrubber(denylist=list(DEFAULT_DENYLIST) + EXTRA_DENY)


SECRET_ENV = ("HELM_WEB_PRESS_KEY", "HELM_APP_TOKEN")


def _walk(o, secrets):
    if isinstance(o, dict):
        for k in list(o):
            if isinstance(k, str) and k.lower() in EXTRA_DENY:
                o[k] = FILTERED
            else:
                o[k] = _walk(o[k], secrets)
        return o
    if isinstance(o, list):
        return [_walk(v, secrets) for v in o]
    if isinstance(o, tuple):
        return tuple(_walk(v, secrets) for v in o)
    if isinstance(o, str):
        for secret in secrets:
            if secret in o:
                o = o.replace(secret, FILTERED)
    return o


def before_send(event, hint=None):
    """Sentry `before_send`: the event with the press key (header or value) and the deployment
    tokens filtered out of every value that could carry one."""
    secrets = [v for v in ((os.environ.get(n) or "").strip() for n in SECRET_ENV) if len(v) >= 8]
    return _walk(event, secrets)
