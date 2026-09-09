"""Log every REJECTED API-token attempt, so a silent 401 is visible.

2026-09-09: deleting an admin account destroyed the token Qurtoba authenticates its
record push with. DRF answers 401 before any view runs, nothing is written, and the
web-server log is not readable by the app account — so the outage was invisible from
here. This patch logs the attempt (path, method, key prefix, client IP) to the same
file the automation writes, and changes no behaviour: the exception is re-raised.
"""
import logging

logger = logging.getLogger(__name__)
_INSTALLED = False


def _log(request, key, reason):
    try:
        from qurtoba.automation.context import log
        head = (key or '')[:8]
        ip = (request.META.get('HTTP_X_FORWARDED_FOR') or request.META.get('REMOTE_ADDR') or '')
        log('api_auth_rejected', None, path=request.path[:80], method=request.method,
            key_head=head, key_len=len(key or ''), ip=str(ip)[:40], reason=reason[:60])
    except Exception:
        logger.warning('api auth rejected on %s (token %s…) — %s', request.path, (key or '')[:8], reason)


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    from rest_framework.authentication import TokenAuthentication
    from rest_framework.exceptions import AuthenticationFailed

    original = TokenAuthentication.authenticate

    def authenticate(self, request):
        try:
            return original(self, request)
        except AuthenticationFailed as exc:
            key = ''
            try:
                raw = request.META.get('HTTP_AUTHORIZATION', '') or ''
                parts = raw.split()
                if len(parts) == 2:
                    key = parts[1]
            except Exception:
                pass
            _log(request, key, str(exc))
            raise

    TokenAuthentication.authenticate = authenticate
    _INSTALLED = True
    logger.info('qurtoba.auth_probe: rejected-token logging installed')
