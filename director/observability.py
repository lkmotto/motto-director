"""Motto fleet observability for variable agents.

Three things in one module:
  1. OpenTelemetry tracing with OTLP export to Langfuse.
  2. FastMCP fleet client that talks to motto-mcp-server.
  3. Helpers (track_run, event, heartbeat) that record into both.

All functions no-op gracefully when env vars aren't set, so this module
is safe to import in dev/test/CI without provisioning anything.

Required to enable Langfuse tracing (BOTH must be set):
    OTEL_EXPORTER_OTLP_ENDPOINT  https://cloud.langfuse.com/api/public/otel
    OTEL_EXPORTER_OTLP_HEADERS   Authorization=Basic <b64(public_key:secret_key)>

Required to enable fleet coordination:
    MOTTO_MCP_URL                e.g. https://motto-mcp.northflank.app/mcp
    MOTTO_MCP_AUTH_TOKEN         shared bearer

Optional:
    AGENT_VERSION, DEPLOY_TARGET, OTEL_SERVICE_NAME
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _loud(event_name: str, **fields: Any) -> None:
    """Emit a structured JSON line on stdout.

    Why not just `logger.warning`? Because Northflank's job log capture is
    reliable for stdout but the deployed director never calls
    `logging.basicConfig()`, so logger.warning calls land on the lastResort
    handler which has been observed to be silently dropped in some
    container configurations. JSON-on-stdout is what the rest of
    director.main uses and it is known to reach NF logs.
    """
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_name,
        **fields,
    }
    try:
        print(json.dumps(record, default=str), flush=True)
    except Exception:
        # Logging must never raise.
        try:
            sys.stdout.write(f"{event_name} {fields}\n")
            sys.stdout.flush()
        except Exception:
            pass

_AGENT_NAME: str | None = None
_KIND: str = "variable"
_TRACER: Any = None


def _otel_enabled() -> bool:
    """Both endpoint and auth headers must be present.

    If endpoint is set but headers aren't (typo, Doppler sync miss),
    every export silently 401s from Langfuse with no warning. Requiring
    both means we cleanly no-op instead of phantom-tracing.
    """
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        and os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    )


def _mcp_enabled() -> bool:
    return bool(os.environ.get("MOTTO_MCP_URL")) and bool(
        os.environ.get("MOTTO_MCP_AUTH_TOKEN")
    )


def init_observability(agent_name: str, kind: str = "variable") -> None:
    """Wire OTel + fleet client. Safe to call multiple times."""
    global _AGENT_NAME, _KIND
    _AGENT_NAME = agent_name
    _KIND = kind

    if _otel_enabled():
        try:
            _setup_otel(agent_name)
        except Exception as e:
            logger.warning("OTel setup failed, continuing without tracing: %s", e)


def _setup_otel(agent_name: str) -> None:
    global _TRACER
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", agent_name),
            "service.version": os.environ.get("AGENT_VERSION", "unknown"),
            "motto.agent.kind": _KIND,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    # Guard against double-init: if a real TracerProvider is already
    # registered, keep it. Otherwise the previous provider's
    # BatchSpanProcessor flush thread is orphaned.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(provider)
        # Flush buffered spans on clean process exit (SIGTERM during
        # Northflank redeploy, container shutdown). Without this the
        # last-window of spans — often the most interesting ones —
        # are dropped silently.
        atexit.register(provider.force_flush)

    _try_instrument("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor")
    _try_instrument("opentelemetry.instrumentation.requests", "RequestsInstrumentor")
    _try_instrument("opentelemetry.instrumentation.anthropic", "AnthropicInstrumentor")

    _TRACER = trace.get_tracer("motto.fleet")


def _try_instrument(module_path: str, cls_name: str) -> None:
    try:
        mod = __import__(module_path, fromlist=[cls_name])
        getattr(mod, cls_name)().instrument()
    except Exception as e:
        # Debug-only: missing package, version skew, or already-instrumented
        # all land here. The log lets you diagnose "why are my httpx spans
        # missing?" without changing behavior.
        logger.debug("Skipping %s.%s instrumentation: %s", module_path, cls_name, e)


def init_fastapi(app: Any) -> None:
    """Auto-instrument a FastAPI app. Call after FastAPI() construction."""
    if not _otel_enabled():
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:
        logger.warning("FastAPI instrumentation failed: %s", e)


@asynccontextmanager
async def _mcp_client():
    """Yield a connected fastmcp Client. Caller checks _mcp_enabled() first."""
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    url = os.environ["MOTTO_MCP_URL"]
    token = os.environ["MOTTO_MCP_AUTH_TOKEN"]
    async with Client(url, auth=BearerAuth(token)) as c:
        yield c


async def register() -> None:
    """Register this agent with the fleet. Idempotent. Call once at startup."""
    if not _mcp_enabled() or not _AGENT_NAME:
        return
    try:
        async with _mcp_client() as c:
            await c.call_tool(
                "register_agent",
                {
                    "name": _AGENT_NAME,
                    "kind": _KIND,
                    "deploy_target": os.environ.get("DEPLOY_TARGET"),
                    "version": os.environ.get("AGENT_VERSION"),
                },
            )
    except Exception as e:
        _loud(
            "observability.register.failed",
            agent=_AGENT_NAME,
            error_type=type(e).__name__,
            error=str(e),
            traceback=traceback.format_exc(limit=5),
        )
        logger.warning("fleet register failed: %s", e)


async def heartbeat(status: dict[str, Any] | None = None) -> None:
    if not _mcp_enabled() or not _AGENT_NAME:
        return
    try:
        async with _mcp_client() as c:
            await c.call_tool(
                "heartbeat", {"agent_name": _AGENT_NAME, "status": status or {}}
            )
    except Exception as e:
        _loud(
            "observability.heartbeat.failed",
            agent=_AGENT_NAME,
            error_type=type(e).__name__,
            error=str(e),
            traceback=traceback.format_exc(limit=5),
        )
        logger.warning("fleet heartbeat failed: %s", e)


class RunHandle:
    __slots__ = ("run_id", "span", "summary", "_token")

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.span: Any = None
        self.summary: dict[str, Any] = {}
        self._token: Any = None


@asynccontextmanager
async def track_run(kind: str, intent: str | None = None):
    """Open a fleet run + Langfuse-linked OTel span; close on exit.

    Child spans (httpx, anthropic) auto-nest under this run's span because
    we attach it as the current OTel context for the duration of the block.
    """
    handle = RunHandle()
    trace_id: str | None = None

    if _TRACER is not None and _AGENT_NAME:
        from opentelemetry import context as otel_context
        from opentelemetry.trace import set_span_in_context

        handle.span = _TRACER.start_span(f"{_AGENT_NAME}.{kind}")
        ctx = handle.span.get_span_context()
        if ctx.is_valid:
            trace_id = format(ctx.trace_id, "032x")
        handle._token = otel_context.attach(set_span_in_context(handle.span))

    if _mcp_enabled() and _AGENT_NAME:
        try:
            async with _mcp_client() as c:
                resp = await c.call_tool(
                    "record_run_start",
                    {
                        "agent_name": _AGENT_NAME,
                        "kind": kind,
                        "intent": intent,
                        "langfuse_trace_id": trace_id,
                    },
                )
                handle.run_id = _extract_run_id(resp)
        except Exception as e:
            _loud(
                "observability.track_run.record_run_start.failed",
                agent=_AGENT_NAME,
                kind=kind,
                intent=intent,
                error_type=type(e).__name__,
                error=str(e),
                traceback=traceback.format_exc(limit=5),
            )
            logger.warning("track_run record_run_start failed: %s", e)

    status = "success"
    try:
        yield handle
    except Exception:
        status = "error"
        raise
    finally:
        if _mcp_enabled() and handle.run_id:
            try:
                async with _mcp_client() as c:
                    await c.call_tool(
                        "record_run_end",
                        {
                            "run_id": handle.run_id,
                            "status": status,
                            "summary": handle.summary,
                        },
                    )
            except Exception as e:
                _loud(
                    "observability.track_run.record_run_end.failed",
                    agent=_AGENT_NAME,
                    run_id=handle.run_id,
                    error_type=type(e).__name__,
                    error=str(e),
                    traceback=traceback.format_exc(limit=5),
                )
                logger.warning("track_run record_run_end failed: %s", e)
        if handle._token is not None:
            try:
                from opentelemetry import context as otel_context
                otel_context.detach(handle._token)
            except Exception:
                pass
        if handle.span is not None:
            try:
                from opentelemetry.trace import Status, StatusCode
                handle.span.set_status(
                    Status(StatusCode.OK if status == "success" else StatusCode.ERROR)
                )
                handle.span.end()
            except Exception:
                pass


async def event(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run: RunHandle | None = None,
    level: str = "info",
) -> None:
    """Record a fleet event. Also added as an OTel span event when run is provided."""
    if run is not None and run.span is not None:
        try:
            run.span.add_event(kind, attributes=_flatten(payload or {}))
        except Exception:
            pass

    if not _mcp_enabled() or not _AGENT_NAME:
        return
    try:
        async with _mcp_client() as c:
            await c.call_tool(
                "record_event",
                {
                    "agent_name": _AGENT_NAME,
                    "kind": kind,
                    "payload": payload or {},
                    "run_id": run.run_id if run else None,
                    "level": level,
                },
            )
    except Exception as e:
        logger.warning("event() failed: %s", e)


def _flatten(d: dict[str, Any]) -> dict[str, Any]:
    """OTel span attributes must be primitives. Stringify nested values."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = v if isinstance(v, str | int | float | bool) else str(v)
    return out


def _extract_run_id(resp: Any) -> str | None:
    data = getattr(resp, "data", None)
    if isinstance(data, dict) and "run_id" in data:
        return data["run_id"]
    content = getattr(resp, "content", None)
    if content:
        try:
            import json as _json
            text = getattr(content[0], "text", None)
            if text:
                parsed = _json.loads(text)
                if isinstance(parsed, dict) and "run_id" in parsed:
                    return parsed["run_id"]
        except Exception:
            pass
    return None
