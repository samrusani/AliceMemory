"""Run an HTTP request a second time when a reader overflowed on a stored row that nests too deeply.

A stored JSON column is read as it was stored (``json_bounds.loads_stored``). A value nested a few hundred levels deep
is walked without trouble by most readers and costs nothing to read. A value nested near the interpreter's recursion
limit makes a recursive reader (the response encoder, the disclosure framing, a canonical form) raise ``RecursionError``
for every caller of every route that returns the row. This layer is the boundary that catches it: when the application
fails with a ``RecursionError`` before it has started a response, the request is run once more, with the same scope and
the same body, in a context where every stored column is decoded by ``loads_bounded``, which cuts the part below 256
levels. The second attempt is the only difference; a request that does not overflow runs once, and pays one wrapper
call and the list of the messages it received.

Only a request that changes nothing a client asked for is run twice: a request that wrote may have committed before the
response failed, and running it again would write it twice. The retry keeps the policy decisions and the context-pack
compile the first attempt committed, and writes neither again. A write route that returns a row too deep to walk fails
as it failed before. A failure of any other kind, a second ``RecursionError`` and a failure after the response has
started are raised as they are.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from alicebot_api.json_bounds import close_read_retry, cutting_deep_json, open_read_retry, overflowed, promote_committed

logger = logging.getLogger(__name__)

# A request that wrote may have committed before the response failed, so only a request that changes nothing a client asked
# for is run twice: every GET, and the POST routes that only compile an answer. A test holds each of these paths to a route
# of the application.
RETRIED_METHODS = frozenset({"GET", "HEAD"})
READ_ONLY_POSTS = frozenset({"/v0/vnext/context-packs"})

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RetryDeepRowMiddleware:
    """A pure ASGI middleware. Register it just inside the body limit, so that it wraps every layer that reads a body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not (
            scope["method"] in RETRIED_METHODS or (scope["method"] == "POST" and scope["path"] in READ_ONLY_POSTS)
        ):
            await self.app(scope, receive, send)
            return

        pristine = dict(scope)
        received: list[Message] = []
        started = False
        # The retry shares this scope with the first attempt, so a policy decision or a context-pack compile the first
        # attempt committed is replayed and not written again. The scope object is the same one the worker thread sees.
        retry_scope, token = open_read_retry()
        try:
            async def recording_receive() -> Message:
                message = await receive()
                received.append(message)
                return message

            async def guarded_send(message: Message) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                await send(message)

            try:
                await self.app(scope, recording_receive, guarded_send)
                return
            except Exception as error:
                if started or not overflowed(error):
                    raise
                logger.warning("A stored row nests too deeply for a reader; the request runs again with the deep part cut")

            promote_committed(retry_scope)
            replay = iter(received)

            async def replaying_receive() -> Message:
                try:
                    return next(replay)
                except StopIteration:
                    return await receive()

            with cutting_deep_json():
                await self.app(pristine, replaying_receive, send)
        finally:
            close_read_retry(token)
