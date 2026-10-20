"""heypdf-renderer: URL or HTML → PDF with Playwright + chromium-headless-shell.

API (over the Unix socket ``HEYP_RENDER_SOCKET``, default /run/renderer/r.sock — the app has no IP route here):
  POST /v1/render   JSON ``RenderRequest`` → 200 PDF bytes + ``X-Render-Info`` (base64 JSON)
                    errors → ``{"error": code, "detail": text}``: 400 invalid_url | bad_request,
                    422 blocked_target | not_a_page | too_many_navigations, 413 too_large, 502 navigation_failed,
                    503 busy (+ Retry-After), 504 timeout
  GET  /healthz     → 200 ``{"status": "ok"}`` once the browser is up (503 before)
  GET  /v1/health   → details (browser version, renders, queue)

Security model (defence in depth, outermost first):
  L0  network: this container sits on an ``internal: true`` network (isolated gateway) whose only peer is the egress
      proxy; the app talks to it over the Unix socket on a shared volume (no IP route to the app at all).
  L1  egress proxy (separate container): resolve once → check every IP → connect to the vetted IP.
  L2  Chromium: all traffic forced through the proxy (also loopback: "<-loopback>"), it may resolve no name but the
      proxy's, QUIC off, WebRTC limited to proxied UDP (= none), service workers blocked, downloads denied; its own
      sandbox is on in production (seccomp profile services/seccomp/chromium-userns.json).
  L3  Playwright: per-request route() check (scheme, request budget), WebSockets mocked, popups closed,
      main-frame navigation budget, page-count and output-size caps, hard deadlines.
  L4  this module's pre-flight URL validation (fast, friendly errors; NOT a security boundary).
Settings: ``HEYP_RENDER_*`` environment variables (see below). Logs never contain URLs.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from playwright.async_api import Browser, Error as PWError, TimeoutError as PWTimeout, async_playwright
from pydantic import BaseModel, ConfigDict, Field, model_validator

from guard import Policy, literal_ip


def _env(name: str, default: str = "") -> str:
    return os.environ.get("HEYP_RENDER_" + name, default)


def _int(name: str, default: int, lo: int = 0, hi: int = 1 << 40) -> int:
    raw = _env(name).strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        sys.exit(f"HEYP_RENDER_{name} must be an integer, got {raw!r}")
    if not lo <= value <= hi:
        sys.exit(f"HEYP_RENDER_{name} must be between {lo} and {hi}, got {value}")
    return value


SOCKET = _env("SOCKET", "/run/renderer/r.sock")
PORT = _int("PORT", 0, 0, 65535)                      # tests only: listen on TCP instead of the socket
EGRESS_HOST = _env("EGRESS_HOST", "egress")
EGRESS_DENY_PORT = _int("EGRESS_DENY_PORT", 3128, 1, 65535)  # launch-level proxy: refuses everything
EGRESS_BASE_PORT = _int("EGRESS_BASE_PORT", 3130, 1, 65000)  # slot i -> port BASE + i
EGRESS_CONTROL_PORT = _int("EGRESS_CONTROL_PORT", 3129, 1, 65535)
EGRESS_SECRET = _env("EGRESS_SECRET")
MAX_CONCURRENT = _int("MAX_CONCURRENT", 2, 1, 16)     # must be <= the egress proxy's HEYP_EGRESS_SLOTS
MAX_QUEUE = _int("MAX_QUEUE", 6, 0, 256)
HARD_TIMEOUT = float(_int("HARD_TIMEOUT", 45, 5, 600))
MAX_REQUESTS = _int("MAX_REQUESTS", 300, 1)
MAX_NAVIGATIONS = _int("MAX_NAVIGATIONS", 10, 1)
MAX_PDF_PAGES = _int("MAX_PDF_PAGES", 50, 1, 1000)
MAX_OUTPUT_BYTES = _int("MAX_OUTPUT_MB", 50, 1, 1024) * 1024 * 1024
MAX_HTML_CHARS = 5_000_000
RECYCLE_AFTER = _int("RECYCLE_AFTER", 200, 1)
SANDBOX = _env("CHROMIUM_SANDBOX", "0") == "1"
CHANNEL = _env("CHANNEL") or None   # None = chromium-headless-shell (the only browser in the image)
CANARY_PORT = _int("LOOPBACK_CANARY", 0, 0, 65535)  # SSRF test bed: a loopback service Chromium must NOT reach
DISABLE_L3 = _env("DISABLE_L3", "0") == "1"           # SSRF test bed: prove the proxy alone holds
PROXY_BYPASS = os.environ.get("HEYP_RENDER_PROXY_BYPASS", "<-loopback>")  # test bed only: "" = Chromium default
EXTRA_ARGS = [a for a in _env("EXTRA_ARGS").split() if a]

CHROMIUM_ARGS = [
    f"--proxy-bypass-list={PROXY_BYPASS}",  # even 127.0.0.1/localhost go through the proxy (and get blocked there)
    # Chromium itself may resolve NOTHING except the proxy host (WebRTC STUN names, prefetch, any future path).
    *([] if _env("NO_RESOLVER_RULES") == "1"  # test bed only
      else [f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {EGRESS_HOST}"]),
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--no-pings",
    "--no-first-run",
    "--deny-permission-prompts",
    "--disable-features=DnsOverHttps,MediaRouter,OptimizationHints,Translate,AcceptCHFrame,InterestFeedContentSuggestions",
    "--js-flags=--max-old-space-size=512",
    "--disable-dev-shm-usage",
]

COOKIE_CSS = """
#onetrust-consent-sdk,#onetrust-banner-sdk,.onetrust-pc-dark-filter,#CybotCookiebotDialog,#CybotCookiebotDialogBodyUnderlay,
#usercentrics-root,#usercentrics-cmp-ui,.qc-cmp2-container,#qc-cmp2-container,#didomi-host,.didomi-popup-backdrop,
#truste-consent-track,.truste_overlay,.truste_box_overlay,#sp-cc,[id^="sp_message_container"],.cc-window,.cc-banner,
.cookie-banner,#cookie-banner,#cookie-notice,#cookie-law-info-bar,.cky-consent-container,.cky-overlay,#cmplz-cookiebanner-container,
.cmplz-cookiebanner,#gdpr-cookie-message,.fc-consent-root,#iubenda-cs-banner,.osano-cm-window,#termly-code-snippet-support,
#axeptio_overlay,#klaro,.klaro,#cookiescript_injected,#moove_gdpr_cookie_info_bar,.evidon-banner,#BorlabsCookieBox,
[aria-label="cookieconsent"],[class*="cookie-consent"],[id*="cookie-consent"],[class*="CookieConsent"],[id*="CookieConsent"]
{display:none!important;visibility:hidden!important}
/* undo CMP scroll locks so the whole page prints (ID-level specificity beats html.sp-message-open-style rules) */
html:not(#heypdf-a):not(#heypdf-b),body:not(#heypdf-a):not(#heypdf-b){overflow:visible!important}
html.sp-message-open body,html[class*="consent"] body,html[class*="cookie"] body,body[class*="consent"],body[class*="cookie"],
body[class*="cmp"],body[class*="no-scroll"],body[class*="noscroll"],body.modal-open,body[style*="position: fixed"]
{position:static!important;top:auto!important}
"""
INIT_SCRIPT = """
(() => {
  // Popups never open (the browser-level 'page' handler is the backstop).
  try { Object.defineProperty(window, 'open', {value: () => null, configurable: false, writable: false}); } catch (e) {}
  // No WebRTC objects at all (L2 policy + L0 network already stop the packets).
  for (const k of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel']) {
    try { Object.defineProperty(window, k, {value: undefined, configurable: false, writable: false}); } catch (e) {}
  }
})();
"""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Margin(_Model):
    top: float = Field(10, ge=0, le=50)
    right: float = Field(10, ge=0, le=50)
    bottom: float = Field(10, ge=0, le=50)
    left: float = Field(10, ge=0, le=50)


class PdfOptions(_Model):
    format: Literal["A3", "A4", "A5", "Letter", "Legal", "Tabloid"] = "A4"
    landscape: bool = False
    margin_mm: Margin = Margin()
    print_background: bool = True
    scale: float = Field(1.0, ge=0.1, le=2.0)
    media: Literal["print", "screen"] = "print"
    single_page: bool = False  # one tall page with the whole document (like a screenshot, but vector)


class RenderRequest(_Model):
    url: str | None = Field(None, max_length=2048)
    html: str | None = Field(None, max_length=MAX_HTML_CHARS)
    viewport_width: int = Field(1366, ge=320, le=3840)
    viewport_height: int = Field(768, ge=240, le=2160)
    device_scale_factor: float = Field(1, ge=1, le=3)
    javascript: bool = True
    delay_ms: int = Field(0, ge=0, le=10_000)
    hide_cookie_banners: bool = True
    pdf: PdfOptions = PdfOptions()
    timeout_ms: int = Field(30_000, ge=1000, le=40_000)

    @model_validator(mode="after")
    def one_source(self):
        if (self.url is None) == (self.html is None):
            raise ValueError("exactly one of url / html")
        return self


class RenderError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


try:
    preflight_policy = Policy(deny_cidrs=_env("DENY_CIDRS"), allow_ports=_env("ALLOW_PORTS") or "80,443",
                              deny_local_nets=True, enabled=_env("POLICY", "enforce") != "off")
except ValueError as e:
    sys.exit(f"heypdf-renderer: bad policy setting: {e}")


def validate_url(raw: str) -> str:
    """L4 pre-flight. Friendly errors only: the egress proxy is the enforcement point."""
    raw = raw.strip()
    if any(c in raw for c in "\r\n\t\x00") or len(raw) > 2048:
        raise RenderError(400, "invalid_url", "control characters or too long")
    if "://" not in raw and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)", raw):
        raw = "https://" + raw
    try:
        u = urllib.parse.urlsplit(raw)
    except ValueError:  # e.g. "http://[::1/" (unbalanced IPv6 brackets)
        raise RenderError(400, "invalid_url", "malformed URL") from None
    if u.scheme not in ("http", "https"):
        raise RenderError(400, "invalid_url", "only http and https URLs can be rendered")
    if u.username is not None or u.password is not None:
        raise RenderError(400, "invalid_url", "URLs with user:password@ are not accepted")
    try:
        host = (u.hostname or "").encode("idna").decode("ascii")
        port = u.port or (443 if u.scheme == "https" else 80)
    except (UnicodeError, ValueError):
        raise RenderError(400, "invalid_url", "bad host or port") from None
    if not host:
        raise RenderError(400, "invalid_url", "missing host")
    reason = preflight_policy.check_name(host) or preflight_policy.check_port(port)
    ip = literal_ip(host)
    if not reason and ip is not None:
        reason = preflight_policy.check_ip(ip)
    if reason:
        raise RenderError(422, "blocked_target", reason)
    return urllib.parse.urlunsplit(u)


_NET_ERROR = re.compile(r"net::ERR_[A-Z0-9_]+")


def navigation_detail(message: str) -> str:
    """Chromium's error code only (``net::ERR_NAME_NOT_RESOLVED``) — never the URL or driver call log."""
    m = _NET_ERROR.search(message or "")
    if m:
        return m.group(0)
    return "the page could not be loaded"


async def egress_slot(method: str, slot: int) -> dict:
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(EGRESS_HOST, EGRESS_CONTROL_PORT), 3)
        w.write(f"{method} /slots/{slot} HTTP/1.1\r\nHost: egress\r\nX-Egress-Secret: {EGRESS_SECRET}\r\n\r\n".encode())
        await w.drain()
        data = await asyncio.wait_for(r.read(), 3)
        w.close()
        return json.loads(data.split(b"\r\n\r\n", 1)[1])
    except Exception as e:  # stats are best-effort (the proxy enforces its budgets on its own)
        return {"error": type(e).__name__}


class Renderer:
    def __init__(self):
        self.pw = None
        self.browser: Browser | None = None
        self.slots: asyncio.Queue[int] = asyncio.Queue()
        for i in range(MAX_CONCURRENT):
            self.slots.put_nowait(i)
        self.waiting = 0
        self.renders = 0
        self.total = 0
        self.lock = asyncio.Lock()
        self.launch_ms = 0.0

    async def start(self):
        self.pw = await async_playwright().start()
        await self.ensure_browser()

    async def stop(self):
        await self.recycle()
        if self.pw is not None:
            with contextlib.suppress(Exception):
                await self.pw.stop()

    async def ensure_browser(self) -> Browser:
        async with self.lock:
            if self.browser is None or not self.browser.is_connected():
                t0 = time.perf_counter()
                self.browser = await self.pw.chromium.launch(
                    headless=True, channel=CHANNEL, chromium_sandbox=SANDBOX, args=CHROMIUM_ARGS + EXTRA_ARGS,
                    # A launch-level proxy too: nothing created outside our contexts can go direct.
                    proxy={"server": f"http://{EGRESS_HOST}:{EGRESS_DENY_PORT}", "bypass": PROXY_BYPASS},
                    handle_sigint=False, handle_sigterm=False, handle_sighup=False)
                self.launch_ms = (time.perf_counter() - t0) * 1000
                self.renders = 0
            return self.browser

    async def recycle(self):
        async with self.lock:
            b, self.browser = self.browser, None
        if b:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(b.close(), 5)

    async def render(self, req: RenderRequest) -> tuple[bytes, str, dict]:
        if self.waiting >= MAX_CONCURRENT + MAX_QUEUE:
            raise RenderError(503, "busy", "renderer queue is full", retry_after=5)
        self.waiting += 1
        try:
            slot = await self.slots.get()  # one egress port per concurrent render = per-job accounting
            try:
                browser = await self.ensure_browser()
                await egress_slot("POST", slot)  # fresh counters / budget for this job
                t0 = time.perf_counter()
                stats: dict = {}
                try:
                    return await asyncio.wait_for(self._render(browser, req, slot), HARD_TIMEOUT)
                except asyncio.TimeoutError:
                    raise RenderError(504, "timeout", "render exceeded the hard deadline") from None
                finally:
                    stats = await egress_slot("DELETE", slot)  # kills lingering sockets of this job
                    self.renders += 1
                    self.total += 1
                    if self.renders >= RECYCLE_AFTER or not browser.is_connected():
                        await self.recycle()
                    print(json.dumps({"event": "render", "slot": slot, "ms": round((time.perf_counter() - t0) * 1000),
                                      "egress": {k: stats.get(k) for k in ("bytes_down", "conns", "over_budget")},
                                      "blocked": len(stats.get("blocked", []))}), flush=True)
            finally:
                self.slots.put_nowait(slot)
        finally:
            self.waiting -= 1

    async def _render(self, browser: Browser, req: RenderRequest, slot: int) -> tuple[bytes, str, dict]:
        url = validate_url(req.url) if req.url is not None else None
        ctx = await browser.new_context(
            viewport={"width": req.viewport_width, "height": req.viewport_height},
            device_scale_factor=req.device_scale_factor,
            java_script_enabled=req.javascript, accept_downloads=False, service_workers="block",
            proxy={"server": f"http://{EGRESS_HOST}:{EGRESS_BASE_PORT + slot}", "bypass": PROXY_BYPASS},
            locale="en-US", timezone_id="UTC", color_scheme="light", reduced_motion="reduce",
            permissions=[], ignore_https_errors=False, bypass_csp=False)
        info = {"requests": 0, "aborted": 0, "navigations": 0, "popups": 0, "main_status": None,
                "bad_scheme": None, "egress_blocked_main": None}
        nav_budget = asyncio.Event()
        try:
            ctx.set_default_timeout(req.timeout_ms)
            if not DISABLE_L3:
                await ctx.add_init_script(INIT_SCRIPT)

                async def on_route(route):
                    info["requests"] += 1
                    scheme = route.request.url.split(":", 1)[0].lower()
                    if scheme not in ("http", "https") or info["requests"] > MAX_REQUESTS:
                        info["aborted"] += 1
                        return await route.abort("blockedbyclient")
                    await route.continue_()

                await ctx.route("**/*", on_route)
                # WebSockets are mocked: the handler never calls ws.connect_to_server(), so no socket is opened.
                await ctx.route_web_socket(re.compile(".*"), lambda ws: None)

                def on_page(p):
                    info["popups"] += 1
                    asyncio.ensure_future(p.close())

            page = await ctx.new_page()
            if not DISABLE_L3:
                ctx.on("page", on_page)

            def on_nav(frame):
                if frame is page.main_frame:
                    info["navigations"] += 1
                    s = frame.url.split(":", 1)[0].lower()
                    if s not in ("http", "https", "about"):
                        info["bad_scheme"] = frame.url[:100]
                    if info["navigations"] > MAX_NAVIGATIONS:
                        nav_budget.set()
            page.on("framenavigated", on_nav)
            page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

            t0 = time.perf_counter()
            resp = None
            try:
                if url is not None:
                    resp = await page.goto(url, wait_until="load", timeout=req.timeout_ms)
                else:
                    await page.set_content(req.html, wait_until="load", timeout=req.timeout_ms)
            except PWTimeout:
                raise RenderError(504, "timeout", "page did not finish loading in time") from None
            except PWError as e:
                msg = str(e).split("\n")[0]
                stats = await egress_slot("GET", slot)
                if stats.get("over_budget"):
                    raise RenderError(413, "too_large", "page exceeded the download budget") from None
                if "Download is starting" in msg:
                    raise RenderError(422, "not_a_page", "the URL is a file download, not a web page") from None
                blocked = stats.get("blocked", [])
                if blocked and ("ERR_TUNNEL_CONNECTION_FAILED" in msg or "ERR_PROXY" in msg or "ERR_EMPTY" in msg):
                    raise RenderError(422, "blocked_target", blocked[-1]["reason"]) from None
                raise RenderError(502, "navigation_failed", navigation_detail(msg)) from None
            if resp is not None:
                info["main_status"] = resp.status
                eg = (await resp.all_headers()).get("x-heypdf-egress", "")
                if eg.startswith("blocked"):  # plain-http hop (e.g. a redirect) refused by the proxy
                    raise RenderError(422, "blocked_target", eg.split("reason=")[-1][:64])
            if info["bad_scheme"]:
                raise RenderError(422, "blocked_target", "navigated to a non-web scheme")
            if nav_budget.is_set():
                raise RenderError(422, "too_many_navigations", "page kept navigating")
            stats = await egress_slot("GET", slot)
            if stats.get("over_budget"):
                raise RenderError(413, "too_large", "page exceeded the download budget")
            if req.hide_cookie_banners and req.javascript:  # add_style_tag needs the main world (JS on);
                with contextlib.suppress(PWError, asyncio.TimeoutError):  # with JS off CMP banners do not render
                    await asyncio.wait_for(page.add_style_tag(content=COOKIE_CSS), 3)
            vw = page.viewport_size["width"]
            if req.delay_ms:
                await asyncio.sleep(req.delay_ms / 1000)
            load_ms = (time.perf_counter() - t0) * 1000

            # CDP layout metrics work with JavaScript disabled (page.evaluate would wait forever for a main world).
            cdp = await ctx.new_cdp_session(page)
            height = int((await cdp.send("Page.getLayoutMetrics"))["cssContentSize"]["height"])
            await cdp.detach()
            info["page_height"] = height
            t1 = time.perf_counter()
            await page.emulate_media(media=req.pdf.media)
            m = req.pdf.margin_mm
            kw = dict(print_background=req.pdf.print_background, scale=req.pdf.scale, tagged=True, outline=True,
                      margin={k: f"{getattr(m, k)}mm" for k in ("top", "right", "bottom", "left")},
                      page_ranges=f"1-{MAX_PDF_PAGES}")
            if req.pdf.single_page:  # one tall sheet, at most 200 in (the PDF page-size limit)
                kw.update(width=f"{vw}px", height=f"{min(height, 200 * 72) + 1}px")
            else:
                kw.update(format=req.pdf.format, landscape=req.pdf.landscape)
            data = await page.pdf(**kw)
            if len(data) > MAX_OUTPUT_BYTES:
                raise RenderError(413, "too_large", f"the result is larger than {MAX_OUTPUT_BYTES >> 20} MB")
            final = urllib.parse.urlsplit(page.url)
            info.update(load_ms=round(load_ms), capture_ms=round((time.perf_counter() - t1) * 1000),
                        final_url=page.url[:2048], final_host=final.hostname or "",
                        egress_bytes=stats.get("bytes_down", 0), egress_blocked=len(stats.get("blocked", [])),
                        viewport_width=vw, device_scale_factor=req.device_scale_factor)
            return data, "application/pdf", info
        finally:
            try:
                await asyncio.wait_for(ctx.close(), 5)
            except Exception:  # a wedged renderer process: throw the whole browser away
                await self.recycle()


R = Renderer()
CANARY_HITS: list[str] = []


@contextlib.asynccontextmanager
async def lifespan(_app):
    if len(EGRESS_SECRET) < 16:
        sys.exit("heypdf-renderer: HEYP_RENDER_EGRESS_SECRET must be set (the egress proxy's control secret)")
    await R.start()
    if CANARY_PORT:
        async def canary(r, w):
            line = (await r.read(1024)).split(b"\r\n", 1)[0]
            CANARY_HITS.append(line.decode("latin-1")[:200])
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\nConnection: close\r\n\r\nLOOPBACK-CANARY")
            await w.drain()
            w.close()
        await asyncio.start_server(canary, "127.0.0.1", CANARY_PORT)
    yield
    await R.stop()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _bad_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    first = (exc.errors() or [{}])[0]
    where = ".".join(str(p) for p in first.get("loc", ()) if p != "body") or "request"
    msg = str(first.get("msg", "invalid value")).removeprefix("Value error, ")
    return JSONResponse({"error": "bad_request", "detail": f"{where}: {msg}"[:300]}, status_code=400)


@app.get("/healthz")
async def healthz():
    b = R.browser
    if b is not None and b.is_connected():
        return {"status": "ok"}
    return JSONResponse({"status": "starting"}, status_code=503)


@app.get("/v1/health")
async def health():
    b = R.browser
    return {"ok": bool(b and b.is_connected()), "version": b.version if b else None, "renders": R.total,
            "waiting": R.waiting, "slots": MAX_CONCURRENT, "queue": MAX_QUEUE, "launch_ms": round(R.launch_ms),
            "sandbox": SANDBOX, **({"canary_hits": CANARY_HITS} if CANARY_PORT else {})}


@app.post("/v1/render")
async def render(req: RenderRequest):
    try:
        data, ctype, info = await R.render(req)
    except RenderError as e:
        headers = {"Retry-After": str(e.extra["retry_after"])} if "retry_after" in e.extra else {}
        return JSONResponse({"error": e.code, "detail": e.detail}, status_code=e.status, headers=headers)
    except Exception as e:  # a crashed page / browser: answer cleanly, recycle, never leak driver output
        print(json.dumps({"event": "render_failed", "error": type(e).__name__}), flush=True)
        await R.recycle()
        return JSONResponse({"error": "navigation_failed", "detail": "the page crashed the renderer"}, status_code=502)
    headers = {"X-Render-Info": base64.b64encode(json.dumps(info).encode()).decode(), "Cache-Control": "no-store"}
    return Response(data, media_type=ctype, headers=headers)


def main() -> None:
    import uvicorn

    common = dict(log_level="warning", access_log=False, server_header=False, date_header=False,
                  timeout_keep_alive=5, limit_concurrency=64)
    if PORT:
        uvicorn.run(app, host="127.0.0.1", port=PORT, **common)
        return
    with contextlib.suppress(FileNotFoundError):
        os.unlink(SOCKET)
    uvicorn.run(app, uds=SOCKET, **common)  # uvicorn makes the socket 0666; the volume is the access boundary


if __name__ == "__main__":
    main()
