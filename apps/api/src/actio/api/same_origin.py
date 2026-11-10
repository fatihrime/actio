"""Refuses requests that change something when another site's page sent them.

The session lives in a cookie, and a browser attaches cookies to requests whichever
page triggers them. Without a check, any website could make a visitor's browser act on
Actio as that visitor (cross-site request forgery).

The browser itself says where a request comes from, in headers a page can't forge:

1. `Sec-Fetch-Site`, sent by every current browser to HTTPS sites and to localhost:
   `same-origin` (our own pages) and `none` (the person typed the address) pass;
   `cross-site` and `same-site` don't. A sibling subdomain is not us.
2. Otherwise `Origin`, which browsers send with every request that isn't a plain GET:
   it must be Actio's own address. This covers a deployment on plain HTTP in a local
   network, where the first header isn't sent.
3. A request with neither header doesn't come from a browser (a script, a command
   line), so no cookie rides along unasked, and it passes.

Reading never needs the check: GET, HEAD and OPTIONS change nothing. The session cookie
is also `SameSite=Lax`, a second barrier in case this one is ever misconfigured.
"""

from starlette.types import ASGIApp, Receive, Scope, Send

from actio.api.problems import problem_response

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class SameOriginOnly:
    """ASGI middleware: under `prefix`, unsafe requests must come from `origin`'s pages."""

    def __init__(self, app: ASGIApp, *, origin: str, prefix: str = "/api/") -> None:
        self._app = app
        self._origin = origin
        self._prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] not in SAFE_METHODS
            and scope["path"].startswith(self._prefix)
            and not self._from_our_pages(scope)
        ):
            refusal = problem_response(
                403, "This request was sent by a page on another site, so it was refused."
            )
            await refusal(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _from_our_pages(self, scope: Scope) -> bool:
        headers: dict[bytes, str] = {
            name: value.decode("latin-1") for name, value in scope["headers"]
        }
        fetch_site = headers.get(b"sec-fetch-site")
        if fetch_site is not None:
            return fetch_site in ("same-origin", "none")
        origin = headers.get(b"origin")
        if origin is not None:
            return origin == self._origin
        return True
