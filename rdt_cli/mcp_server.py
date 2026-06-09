"""Remote MCP server for rdt-cli.

This module exposes read-only Reddit tools over Streamable HTTP. It reuses the
rdt-cli client and parsers directly instead of invoking the Click CLI, so stdout
stays reserved for MCP transports.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MCP_PATH = "/mcp"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000
DEFAULT_TOOL_COUNT = 20
MAX_TOOL_COUNT = 50

SUBREDDIT_SORTS = {"hot", "new", "top", "rising", "controversial"}
SEARCH_SORTS = {"relevance", "hot", "top", "new", "comments"}
TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}
COMMENT_SORTS = {"best", "top", "new", "controversial", "old", "qa"}

AsgiMessage = MutableMapping[str, Any]
AsgiScope = MutableMapping[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]
AsgiApp = Callable[[AsgiScope, AsgiReceive, AsgiSend], Awaitable[None]]


@dataclass(frozen=True)
class McpSettings:
    """Runtime settings for the remote MCP server."""

    host: str = DEFAULT_MCP_HOST
    port: int = DEFAULT_MCP_PORT
    path: str = DEFAULT_MCP_PATH
    api_keys: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    allow_any_origin: bool = False
    log_level: str = "info"


class ApiKeyOriginMiddleware:
    """ASGI middleware that enforces API key and Origin checks."""

    def __init__(self, app: AsgiApp, settings: McpSettings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(
        self,
        scope: AsgiScope,
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        headers = _headers_to_dict(scope.get("headers", []))
        origin = headers.get("origin")
        if not is_origin_allowed(origin, self._settings):
            await _send_json_error(send, 403, "forbidden_origin")
            return

        if not is_request_authorized(headers, self._settings.api_keys):
            await _send_json_error(send, 401, "unauthorized")
            return

        await self._app(scope, receive, send)


def load_settings_from_env() -> McpSettings:
    """Load MCP server settings from environment variables."""
    allowed_origins = _split_csv_env("RDT_MCP_ALLOWED_ORIGINS")
    return McpSettings(
        host=os.environ.get("RDT_MCP_HOST", DEFAULT_MCP_HOST).strip() or DEFAULT_MCP_HOST,
        port=_parse_port(os.environ.get("RDT_MCP_PORT"), DEFAULT_MCP_PORT),
        path=_normalize_path(os.environ.get("RDT_MCP_PATH", DEFAULT_MCP_PATH)),
        api_keys=_load_api_keys_from_env(),
        allowed_hosts=_build_allowed_hosts(
            _split_csv_env("RDT_MCP_ALLOWED_HOSTS"),
            allowed_origins,
        ),
        allowed_origins=allowed_origins,
        allow_any_origin=_parse_bool(os.environ.get("RDT_MCP_ALLOW_ANY_ORIGIN")),
        log_level=os.environ.get("RDT_MCP_LOG_LEVEL", "info").strip().lower() or "info",
    )


def is_request_authorized(headers: dict[str, str], api_keys: Iterable[str]) -> bool:
    """Return True when headers contain a configured API key."""
    keys = tuple(key for key in api_keys if key)
    if not keys:
        return False

    candidates = []
    auth_header = headers.get("authorization", "")
    prefix = "bearer "
    if auth_header.lower().startswith(prefix):
        candidates.append(auth_header[len(prefix):].strip())

    for header_name in ("x-api-key", "api-key", "api_key"):
        value = headers.get(header_name, "")
        if value:
            candidates.append(value.strip())

    return any(
        hmac.compare_digest(candidate, key)
        for candidate in candidates
        for key in keys
    )


def is_origin_allowed(origin: str | None, settings: McpSettings) -> bool:
    """Return True when the Origin header is acceptable for this server."""
    if not origin:
        return True
    if settings.allow_any_origin:
        return True
    return origin.strip().rstrip("/") in settings.allowed_origins


def create_mcp_server(path: str = DEFAULT_MCP_PATH, settings: McpSettings | None = None) -> Any:
    """Create and register the read-only FastMCP server."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only without dependency
        raise RuntimeError(
            "MCP support requires the 'mcp' package. Run `uv sync` or install rdt-cli "
            "with its runtime dependencies."
        ) from exc

    settings = settings or McpSettings(
        path=_normalize_path(path),
        allowed_hosts=_default_allowed_hosts(),
    )
    server = _new_fastmcp(FastMCP, settings)

    @server.tool()
    def health() -> dict[str, Any]:
        """Check whether rdt-cli has a usable Reddit credential."""
        from .auth import get_credential

        credential = get_credential()
        if credential is None:
            return _success({"authenticated": False, "credential_present": False})

        def run(client: Any) -> dict[str, Any]:
            status = client.validate_session()
            return {
                "credential_present": True,
                "source": credential.source,
                "username_present": bool(credential.username or status.get("username")),
                "status": status,
            }

        return _tool_response(run, require_credential=True)

    @server.tool()
    def whoami() -> dict[str, Any]:
        """Return the currently authenticated Reddit user."""
        return _tool_response(lambda client: {"user": client.get_me()}, require_credential=True)

    @server.tool()
    def browse_subreddit(
        subreddit: str,
        sort: str = "hot",
        time_filter: str | None = None,
        count: int = DEFAULT_TOOL_COUNT,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Browse a subreddit, r/all, or r/popular."""
        def run(client: Any) -> dict[str, Any]:
            name = _normalize_subreddit(subreddit)
            limit = _resolve_count(count)
            if name == "all":
                data = client.get_all(limit=limit, after=after)
            elif name == "popular":
                data = client.get_popular(limit=limit, after=after)
            else:
                sort_name = _validate_choice(sort, SUBREDDIT_SORTS, "sort")
                time_name = _validate_optional_choice(time_filter, TIME_FILTERS, "time_filter")
                data = client.get_subreddit(
                    name,
                    sort=sort_name,
                    limit=limit,
                    after=after,
                    time_filter=time_name,
                )
            return _listing_payload(data)

        return _tool_response(run)

    @server.tool()
    def browse_home_feed(
        count: int = DEFAULT_TOOL_COUNT,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Browse the authenticated user's Reddit home feed."""
        return _tool_response(
            lambda client: _listing_payload(client.get_home(limit=_resolve_count(count), after=after)),
            require_credential=True,
        )

    @server.tool()
    def search_reddit(
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "all",
        count: int = DEFAULT_TOOL_COUNT,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Search Reddit globally or within one subreddit."""
        def run(client: Any) -> dict[str, Any]:
            query_text = query.strip()
            if not query_text:
                raise ValueError("query is required")
            data = client.search(
                query=query_text,
                subreddit=_normalize_optional_subreddit(subreddit),
                sort=_validate_choice(sort, SEARCH_SORTS, "sort"),
                time_filter=_validate_choice(time_filter, TIME_FILTERS, "time_filter"),
                limit=_resolve_count(count),
                after=after,
            )
            payload = _listing_payload(data)
            payload["query"] = query_text
            return payload

        return _tool_response(run)

    @server.tool()
    def get_post_details(
        post_id: str,
        sort: str = "best",
        count: int = DEFAULT_TOOL_COUNT,
        expand_more: bool = False,
    ) -> dict[str, Any]:
        """Fetch a Reddit post and its comments by id or Reddit URL."""
        def run(client: Any) -> dict[str, Any]:
            from .commands._common import compact_post_detail
            from .commands.post import _attach_more_comments
            from .parser import parse_morechildren_response, parse_post_detail

            normalized_id = _normalize_post_id(post_id)
            sort_name = _validate_choice(sort, COMMENT_SORTS, "sort")
            raw = client.get_post_comments(
                post_id=normalized_id,
                sort=sort_name,
                limit=_resolve_count(count),
            )
            detail = parse_post_detail(raw)
            if expand_more and detail.more_children:
                expanded = client.get_more_comments(normalized_id, detail.more_children, sort=sort_name)
                detail = _attach_more_comments(detail, parse_morechildren_response(expanded))
            return compact_post_detail(detail)

        return _tool_response(run)

    @server.tool()
    def get_subreddit_info(subreddit: str) -> dict[str, Any]:
        """Fetch subreddit metadata."""
        def run(client: Any) -> dict[str, Any]:
            from .parser import parse_subreddit_info

            info = parse_subreddit_info(client.get_subreddit_about(_normalize_subreddit(subreddit)))
            return {"subreddit": info.to_dict()}

        return _tool_response(run)

    @server.tool()
    def user_analysis(username: str, count: int = 10) -> dict[str, Any]:
        """Fetch a user's profile, recent posts, and recent comments."""
        def run(client: Any) -> dict[str, Any]:
            from .parser import parse_user_profile

            user = _normalize_username(username)
            limit = _resolve_count(count)
            profile = parse_user_profile(client.get_user_about(user))
            posts = _listing_payload(client.get_user_posts(user, limit=limit))
            comments = _listing_children_data(client.get_user_comments(user, limit=limit))
            return {
                "user": profile.to_dict(),
                "posts": posts["items"],
                "comments": comments,
            }

        return _tool_response(run)

    @server.tool()
    def get_saved(count: int = DEFAULT_TOOL_COUNT, after: str | None = None) -> dict[str, Any]:
        """Fetch saved items for the authenticated Reddit user."""
        def run(client: Any) -> dict[str, Any]:
            username = _resolve_current_username(client)
            return _listing_payload(client.get_user_saved(username, limit=_resolve_count(count), after=after))

        return _tool_response(run, require_credential=True)

    @server.tool()
    def get_upvoted(count: int = DEFAULT_TOOL_COUNT, after: str | None = None) -> dict[str, Any]:
        """Fetch upvoted items for the authenticated Reddit user."""
        def run(client: Any) -> dict[str, Any]:
            username = _resolve_current_username(client)
            return _listing_payload(client.get_user_upvoted(username, limit=_resolve_count(count), after=after))

        return _tool_response(run, require_credential=True)

    @server.tool()
    def reddit_explain(topic: str) -> dict[str, Any]:
        """Explain common Reddit terms and rdt-cli MCP behavior."""
        explanations = {
            "karma": "Reddit score associated with posts and comments. It is not a precise vote count.",
            "subreddit": "A topic community named like r/python or r/LocalLLaMA.",
            "sort": "Common listing sorts include hot, new, top, rising, and controversial.",
            "time_filter": "For top or controversial sorts, use hour, day, week, month, year, or all.",
            "more_comments": (
                "Reddit comment trees can contain placeholders. Use expand_more for more "
                "top-level comments."
            ),
            "mcp_auth": "The MCP API key protects the remote server and is separate from Reddit cookies.",
        }
        key = topic.strip().lower().replace(" ", "_")
        explanation = explanations.get(key, "No local explanation is available for this topic.")
        return _success({"topic": topic, "explanation": explanation})

    return server


def create_asgi_app(settings: McpSettings | None = None) -> Any:
    """Create the authenticated ASGI app mounted at the configured MCP path."""
    settings = settings or load_settings_from_env()
    _require_api_key(settings)

    server = create_mcp_server(settings=settings)
    inner_app = server.streamable_http_app()
    return ApiKeyOriginMiddleware(inner_app, settings)


def main() -> None:
    """Run the remote HTTP MCP server."""
    settings = load_settings_from_env()
    _require_api_key(settings)

    import uvicorn

    app = create_asgi_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level)


def _tool_response(
    action: Callable[[Any], dict[str, Any]],
    *,
    require_credential: bool = False,
) -> dict[str, Any]:
    from .auth import get_credential
    from .commands._common import error_payload, success_payload
    from .exceptions import RedditApiError, error_code_for_exception

    try:
        credential = get_credential()
        if require_credential and credential is None:
            raise RuntimeError("No Reddit credential found. Run `rdt login` first.")
        from .client import RedditClient

        with RedditClient(credential) as client:
            return success_payload(action(client))
    except (RedditApiError, RuntimeError, ValueError) as exc:
        code = error_code_for_exception(exc) if isinstance(exc, RedditApiError) else "api_error"
        return error_payload(code, str(exc))


def _success(data: dict[str, Any]) -> dict[str, Any]:
    from .commands._common import success_payload

    return success_payload(data)


def _new_fastmcp(fastmcp_cls: Any, settings: McpSettings) -> Any:
    kwargs = {
        "host": settings.host,
        "port": settings.port,
        "stateless_http": True,
        "json_response": True,
        "streamable_http_path": settings.path,
        "transport_security": _transport_security_settings(settings),
    }
    try:
        server = fastmcp_cls("rdt-cli", **kwargs)
    except TypeError:
        server = fastmcp_cls("rdt-cli")
        _configure_fastmcp_settings(server, kwargs)
    else:
        _configure_fastmcp_settings(server, kwargs)
    return server


def _configure_fastmcp_settings(server: Any, settings: dict[str, Any]) -> None:
    fastmcp_settings = getattr(server, "settings", None)
    if fastmcp_settings is None:
        return
    for name, value in settings.items():
        if hasattr(fastmcp_settings, name):
            try:
                setattr(fastmcp_settings, name, value)
            except Exception:
                logger.debug("Unable to set FastMCP setting %s", name, exc_info=True)


def _transport_security_settings(settings: McpSettings) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=not settings.allow_any_origin,
        allowed_hosts=list(settings.allowed_hosts or _default_allowed_hosts()),
        allowed_origins=list(settings.allowed_origins),
    )


def _listing_payload(data: dict[str, Any]) -> dict[str, Any]:
    from .parser import parse_listing

    listing = parse_listing(data)
    return {
        "items": [item.to_dict() for item in listing.items],
        "pagination": {
            "after": listing.after,
            "before": listing.before,
        },
    }


def _listing_children_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    children = data.get("data", {}).get("children", [])
    return [child.get("data", child) for child in children]


def _resolve_count(count: int | None) -> int:
    if count is None:
        return DEFAULT_TOOL_COUNT
    try:
        value = int(count)
    except (TypeError, ValueError) as exc:
        raise ValueError("count must be an integer") from exc
    if value <= 0:
        raise ValueError("count must be greater than 0")
    return min(value, MAX_TOOL_COUNT)


def _normalize_subreddit(value: str) -> str:
    subreddit = value.strip().removeprefix("r/").lstrip("/")
    if not subreddit:
        raise ValueError("subreddit is required")
    return subreddit


def _normalize_optional_subreddit(value: str | None) -> str | None:
    if value is None:
        return None
    return _normalize_subreddit(value)


def _normalize_username(value: str) -> str:
    username = value.strip().removeprefix("u/").lstrip("@")
    if not username:
        raise ValueError("username is required")
    return username


def _normalize_post_id(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("post_id is required")
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        match = re.search(r"/comments/([^/?#]+)", parsed.path)
        if match:
            return match.group(1)
        if parsed.netloc == "redd.it":
            candidate = parsed.path.strip("/").split("/", 1)[0]
            if candidate:
                return candidate
        raise ValueError("post_id must be a Reddit comments URL, redd.it URL, or bare post id")
    return raw.removeprefix("t3_")


def _validate_choice(value: str, allowed: Iterable[str], field_name: str) -> str:
    normalized = value.strip().lower() if value else ""
    if normalized not in set(allowed):
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {choices}")
    return normalized


def _validate_optional_choice(
    value: str | None,
    allowed: Iterable[str],
    field_name: str,
) -> str | None:
    if value is None or not value.strip():
        return None
    return _validate_choice(value, allowed, field_name)


def _resolve_current_username(client: Any) -> str:
    identity = client.get_me()
    username = identity.get("name") or client.session.username
    if not username:
        raise RuntimeError("Unable to resolve current username from session")
    return username


def _headers_to_dict(headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in headers
    }


async def _send_json_error(send: AsgiSend, status: int, code: str) -> None:
    body = f'{{"error":"{code}"}}'.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _load_api_keys_from_env() -> tuple[str, ...]:
    values = []
    single_key = os.environ.get("RDT_MCP_API_KEY", "").strip()
    if single_key:
        values.append(single_key)
    values.extend(_split_csv_env("RDT_MCP_API_KEYS"))
    return tuple(dict.fromkeys(value for value in values if value))


def _build_allowed_hosts(
    explicit_hosts: Iterable[str],
    allowed_origins: Iterable[str],
) -> tuple[str, ...]:
    values = list(_default_allowed_hosts())
    for host in explicit_hosts:
        _append_host_patterns(values, host)
    for origin in allowed_origins:
        parsed = urllib.parse.urlparse(origin)
        if parsed.netloc:
            _append_host_patterns(values, parsed.netloc)
    return tuple(dict.fromkeys(value for value in values if value))


def _default_allowed_hosts() -> tuple[str, ...]:
    return ("127.0.0.1:*", "localhost:*", "[::1]:*")


def _append_host_patterns(values: list[str], raw_host: str) -> None:
    host = raw_host.strip().removeprefix("http://").removeprefix("https://").rstrip("/")
    if not host:
        return
    values.append(host)
    if not host.endswith(":*") and ":" not in host:
        values.append(f"{host}:*")


def _split_csv_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


def _parse_port(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError("RDT_MCP_PORT must be an integer") from exc
    if port < 1 or port > 65535:
        raise RuntimeError("RDT_MCP_PORT must be between 1 and 65535")
    return port


def _parse_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_path(raw: str | None) -> str:
    path = (raw or DEFAULT_MCP_PATH).strip() or DEFAULT_MCP_PATH
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def _require_api_key(settings: McpSettings) -> None:
    if not settings.api_keys:
        raise RuntimeError("Set RDT_MCP_API_KEY before starting the remote MCP server.")


if __name__ == "__main__":
    main()
