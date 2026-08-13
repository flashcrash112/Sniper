"""Off-chain metadata fetching.

The one unavoidable latency cost in this bot. The ticker and name arrive
on-chain inside the create event, but the socials — the Twitter link we match
against — live in a JSON document behind the `uri` field, usually on IPFS. That
means an HTTP round trip mid-decision.

Three things keep the damage bounded:

* a hard timeout (`[target] metadata_timeout_ms`) after which we give up and
  score the Twitter signal as unverified rather than waiting,
* a pre-warmed connection pool, so the common gateways are already through DNS,
  TCP and TLS before the first launch arrives, and
* racing the configured gateways for `ipfs://` URIs, taking the first response.

If you are not matching on Twitter at all, none of this runs and the decision
stays entirely on-chain.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional, Sequence

import aiohttp

from .logging_setup import get_logger

log = get_logger("sniper.metadata")

_IPFS_PREFIX = "ipfs://"
_CID_IN_PATH = re.compile(r"/ipfs/([A-Za-z0-9]+)")


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """The socials block of a pump.fun metadata document."""

    twitter: Optional[str] = None
    telegram: Optional[str] = None
    website: Optional[str] = None
    description: Optional[str] = None
    fetch_ms: float = 0.0
    source: Optional[str] = None

    @property
    def resolved(self) -> bool:
        """Whether the document was actually retrieved."""
        return self.source is not None


UNRESOLVED = TokenMetadata()


class MetadataFetcher:
    """Fetches and normalises token metadata documents."""

    def __init__(
        self,
        gateways: Sequence[str],
        timeout_ms: int,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._gateways = list(gateways)
        self._timeout = timeout_ms / 1000.0
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "MetadataFetcher":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                connector=aiohttp.TCPConnector(
                    limit=32, ttl_dns_cache=600, keepalive_timeout=120
                ),
                headers={"accept": "application/json"},
            )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def warm(self) -> None:
        """Open connections to the gateways ahead of time.

        Pays DNS + TCP + TLS now so the first real fetch does not. Failures are
        ignored: a cold gateway is a slower fetch, not a broken bot.
        """
        if not self._session:
            return

        async def touch(url: str) -> None:
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=2)):
                    pass
            except Exception:
                pass

        await asyncio.gather(*(touch(g) for g in self._gateways), return_exceptions=True)

    def _candidate_urls(self, uri: str) -> list[str]:
        """Every URL worth trying for this metadata URI, best first."""
        uri = uri.strip()
        if uri.startswith(_IPFS_PREFIX):
            cid = uri[len(_IPFS_PREFIX) :]
            return [g.rstrip("/") + "/" + cid.lstrip("/") for g in self._gateways]
        if uri.startswith(("http://", "https://")):
            urls = [uri]
            # A gateway can be down or rate-limiting; if we can recover the CID
            # we can ask a different one for the same document.
            match = _CID_IN_PATH.search(uri)
            if match:
                cid = match.group(1)
                urls += [
                    g.rstrip("/") + "/" + cid
                    for g in self._gateways
                    if not uri.startswith(g.rstrip("/"))
                ]
            return urls
        return []

    async def _fetch_one(self, url: str) -> Optional[dict]:
        assert self._session is not None
        async with self._session.get(url) as response:
            if response.status != 200:
                return None
            # pump.fun metadata is served with assorted content types; parse
            # regardless rather than trusting the header.
            return await response.json(content_type=None)

    async def fetch(self, uri: str) -> TokenMetadata:
        """Fetch and parse metadata, returning :data:`UNRESOLVED` on failure.

        Never raises: a metadata failure must degrade the match score, not kill
        the listener.
        """
        urls = self._candidate_urls(uri)
        if not urls or self._session is None:
            return UNRESOLVED

        loop = asyncio.get_running_loop()
        started = loop.time()

        tasks = [asyncio.create_task(self._fetch_one(url)) for url in urls]
        try:
            remaining = set(tasks)
            deadline = started + self._timeout
            while remaining:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                done, remaining = await asyncio.wait(
                    remaining, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        document = task.result()
                    except Exception:
                        continue
                    if isinstance(document, dict):
                        elapsed = (loop.time() - started) * 1000
                        index = tasks.index(task)
                        return _parse_document(document, urls[index], elapsed)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        log.debug(
            "metadata_unresolved",
            extra={"uri": uri, "elapsed_ms": round((loop.time() - started) * 1000, 1)},
        )
        return UNRESOLVED


def _first_str(document: dict, *keys: str) -> Optional[str]:
    """First non-empty string among `keys`, checking nested `extensions` too."""
    sources = [document]
    extensions = document.get("extensions")
    if isinstance(extensions, dict):
        sources.append(extensions)
    properties = document.get("properties")
    if isinstance(properties, dict):
        sources.append(properties)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _parse_document(document: dict, source: str, elapsed_ms: float) -> TokenMetadata:
    return TokenMetadata(
        twitter=_first_str(document, "twitter", "twitter_url", "x"),
        telegram=_first_str(document, "telegram", "telegram_url"),
        website=_first_str(document, "website", "external_url"),
        description=_first_str(document, "description"),
        fetch_ms=round(elapsed_ms, 1),
        source=source,
    )
