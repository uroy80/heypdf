"""The real anthropic SDK (sync client, 1.x on httpx2) over httpx2.MockTransport: the exact requests heypdf sends and how
responses (SSE stream, JSON, refusals, errors) are handled. No network, no API key."""
from __future__ import annotations

import json
import threading

import httpx2
import pytest
from anthropic import DefaultHttpxClient

from app.ai import prompts, summarize
from app.ai.budget import Ledger
from app.ai.llm import FALLBACK_BETA, AIError, AnthropicLLM, Final, metered
from app.ai.pricing import load_prices


STREAM_HEADERS = {"content-type": "text/event-stream"}


def sse(*events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def stream_body(model="claude-opus-5-5", text_parts=("Hello ", "world."), stop="end_turn", usage=None) -> bytes:
    usage = usage or {"input_tokens": 50, "output_tokens": 1, "cache_creation_input_tokens": 1200,
                      "cache_read_input_tokens": 0}
    evs = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                                  "model": model, "content": [], "stop_reason": None,
                                                  "stop_sequence": None, "usage": usage}},
           {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "",
                                                                         "signature": ""}},
           {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
           {"type": "content_block_stop", "index": 0},
           {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}]
    evs += [{"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": t}}
            for t in text_parts]
    evs += [{"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
             "usage": {"output_tokens": 420}},
            {"type": "message_stop"}]
    return sse(*evs)


def message_json(text, model="claude-opus-5-5", stop="end_turn", usage=None, stop_details=None) -> dict:
    return {"id": "msg_2", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}],
            "stop_reason": stop, "stop_sequence": None, "stop_details": stop_details,
            "usage": usage or {"input_tokens": 900, "output_tokens": 300, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 4000}}


def as_sse(msg: dict) -> httpx2.Response:
    """The SSE stream the API sends for the complete message ``msg`` (every generating call is streamed)."""
    usage = dict(msg["usage"])
    start = {**msg, "content": [], "stop_reason": None, "stop_sequence": None, "stop_details": None,
             "usage": {**{k: v for k, v in usage.items() if k != "iterations"}, "output_tokens": 1}}
    evs: list[dict] = [{"type": "message_start", "message": start}]
    for i, block in enumerate(msg["content"]):
        if block["type"] == "thinking":
            evs += [{"type": "content_block_start", "index": i,
                     "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                    {"type": "content_block_delta", "index": i,
                     "delta": {"type": "signature_delta", "signature": block.get("signature", "s")}}]
        else:
            evs.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            if block["text"]:
                evs.append({"type": "content_block_delta", "index": i,
                            "delta": {"type": "text_delta", "text": block["text"]}})
        evs.append({"type": "content_block_stop", "index": i})
    evs += [{"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None,
                                                "stop_details": msg.get("stop_details")}, "usage": usage},
            {"type": "message_stop"}]
    return httpx2.Response(200, headers=STREAM_HEADERS, content=sse(*evs))


class Recorder:
    def __init__(self, responder):
        self.requests: list = []
        self.responder = responder

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request, body))
        return self.responder(request, body)


def make_llm(responder, **kw):
    rec = Recorder(responder)
    http = DefaultHttpxClient(transport=httpx2.MockTransport(rec))
    llm = AnthropicLLM(api_key="sk-ant-test", http_client=http, max_retries=kw.pop("max_retries", 0), **kw)
    return llm, rec


SYSTEM = [{"type": "text", "text": prompts.SYSTEM_SUMMARIZE}]
MSGS = summarize.single_messages('<document pages="1">\n<page n="1">\nhi\n</page>\n</document>',
                                 summarize.SummaryOptions("short"))


def test_count_tokens_request():
    llm, rec = make_llm(lambda req, body: httpx2.Response(200, json={"input_tokens": 1234}))
    n = llm.count_tokens(model="claude-opus-5-5", system=SYSTEM, messages=MSGS)
    req, body = rec.requests[0]
    assert n == 1234 and req.url.path == "/v1/messages/count_tokens"
    assert body == {"model": "claude-opus-5-5", "system": SYSTEM, "messages": MSGS}
    assert req.headers["x-api-key"] == "sk-ant-test" and "anthropic-version" in req.headers


def test_stream_request_and_parsing():
    llm, rec = make_llm(lambda req, body: httpx2.Response(200, headers=STREAM_HEADERS, content=stream_body()))
    texts: list[str] = []
    final = llm.stream(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, max_tokens=6000, effort="medium",
                       on_text=texts.append)
    req, body = rec.requests[0]
    assert req.url.path == "/v1/messages" and body["stream"] is True
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 6000
    assert body["output_config"] == {"effort": "medium"}
    assert "thinking" not in body and "temperature" not in body           # adaptive by default; no sampling params
    assert "inference_geo" not in body
    assert body["fallbacks"] == "default" and FALLBACK_BETA in req.headers["anthropic-beta"]
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert texts == ["Hello ", "world."] and isinstance(final, Final) and final.text == "Hello world."
    assert (final.usage.input_tokens, final.usage.output_tokens, final.usage.cache_write) == (50, 420, 1200)
    assert final.usage.cost(load_prices(), "claude-opus-5-5") == pytest.approx((50 * 4 + 420 * 20 + 1200 * 5) / 1e6)


def test_inference_geo_is_a_top_level_parameter():
    llm, rec = make_llm(lambda req, body: as_sse(message_json('{"notes": "x"}')),
                        inference_geo="us")
    llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema=summarize.NOTES_SCHEMA,
                      max_tokens=4000, effort="low")
    assert rec.requests[0][1]["inference_geo"] == "us"


def test_haiku_gets_no_fallbacks():
    llm, rec = make_llm(lambda req, body: as_sse(message_json('{"notes": "x"}',
                                                                                  model="claude-haiku-5-5")))
    llm.complete_json(model="claude-haiku-5-5", system=SYSTEM, messages=MSGS, schema=summarize.NOTES_SCHEMA,
                      max_tokens=4000, effort="low")
    req, body = rec.requests[0]
    assert "fallbacks" not in body and "anthropic-beta" not in req.headers


def test_fallbacks_can_be_switched_off():
    llm, rec = make_llm(lambda req, body: as_sse(message_json('{"notes": "x"}')), fallbacks=False)
    llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={}, max_tokens=10, effort="low")
    assert "fallbacks" not in rec.requests[0][1]


def test_complete_json_structured_output():
    llm, rec = make_llm(lambda req, body: as_sse(
        message_json(json.dumps({"translations": [{"id": "u1", "text": "Hallo"}]}))))
    data, usage, stop = llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS,
                                          schema={"type": "object"}, max_tokens=3000, effort="low")
    _, body = rec.requests[0]
    assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert body["stream"] is True and "thinking" not in body       # streamed, so a cancelled job can stop it
    assert data == {"translations": [{"id": "u1", "text": "Hallo"}]} and usage.cache_read == 4000


def test_truncated_or_invalid_json_returns_none():
    llm, _ = make_llm(lambda req, body: as_sse(message_json('{"transl', stop="max_tokens")))
    data, _, stop = llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={},
                                      max_tokens=10, effort="low")
    assert data is None and stop == "max_tokens"
    llm, _ = make_llm(lambda req, body: as_sse(message_json("not json at all")))
    data, _, stop = llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={},
                                      max_tokens=10, effort="low")
    assert data is None and stop == "invalid_json"


def test_refusal_raises_with_usage_for_the_ledger(tmp_path):
    llm, _ = make_llm(lambda req, body: as_sse(message_json(
        "", stop="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"})))
    job = Ledger(tmp_path / "l.json", 10).job(1)
    prices = load_prices()
    with pytest.raises(AIError) as e:
        metered(job, 0.5, prices, "claude-opus-5-5",
                lambda: llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={},
                                          max_tokens=10, effort="low"), lambda r: r[1])
    assert e.value.kind == "refused" and e.value.status == 422 and "cyber" in e.value.message
    expected = (900 * 4 + 300 * 20 + 4000 * 0.2) / 1e6
    assert job.spent_usd == pytest.approx(expected) and not job.open


def test_streamed_refusal_raises():
    llm, _ = make_llm(lambda req, body: httpx2.Response(200, headers=STREAM_HEADERS,
                                                        content=stream_body(text_parts=("Part",), stop="refusal")))
    with pytest.raises(AIError) as e:
        llm.stream(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, max_tokens=100, effort="low",
                   on_text=lambda t: None)
    assert e.value.kind == "refused"


def test_fallback_hop_usage_is_priced_per_model():
    usage = {"input_tokens": 900, "output_tokens": 300, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0,
             "iterations": [
                 {"type": "message", "model": "claude-opus-5-5", "input_tokens": 900, "output_tokens": 20,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                 {"type": "fallback_message", "model": "claude-opus-5", "input_tokens": 900, "output_tokens": 300,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}]}
    llm, _ = make_llm(lambda req, body: as_sse(message_json('{"notes":"n"}', model="claude-opus-5",
                                                                                usage=usage)))
    _, u, _ = llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={}, max_tokens=4000,
                                effort="low")
    assert u.model == "claude-opus-5" and len(u.hops) == 2
    assert u.cost(load_prices(), "claude-opus-5-5") == pytest.approx((900 * 4 + 20 * 20 + 900 * 5 + 300 * 25) / 1e6)


def test_rejected_fallbacks_are_retried_once_without_them():
    def responder(req, body):
        if "fallbacks" in body:
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                         "message": "fallbacks not allowed"}})
        return as_sse(message_json('{"notes": "ok"}'))
    llm, rec = make_llm(responder)
    data, _, _ = llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={},
                                   max_tokens=100, effort="low")
    assert data == {"notes": "ok"} and len(rec.requests) == 2
    llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={}, max_tokens=100, effort="low")
    assert len(rec.requests) == 3 and "fallbacks" not in rec.requests[-1][1]     # remembered for the process


def test_other_bad_requests_are_not_retried():
    llm, rec = make_llm(lambda req, body: httpx2.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error", "message": "nope"}}), fallbacks=False)
    with pytest.raises(AIError) as e:
        llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={}, max_tokens=10,
                          effort="low")
    assert e.value.status == 502 and len(rec.requests) == 1


@pytest.mark.parametrize("status,headers,kind,http", [
    (401, {}, "unavailable", 503), (403, {}, "unavailable", 503), (404, {}, "unavailable", 503),
    (429, {"retry-after": "17"}, "busy", 503), (529, {}, "busy", 503), (500, {}, "busy", 503),
    (400, {}, "upstream", 502), (413, {}, "too_large", 413), (402, {}, "unavailable", 503)])
def test_error_mapping(status, headers, kind, http):
    err = {"type": "error", "error": {"type": "x_error", "message": "secret upstream detail"}, "request_id": "req_1"}
    llm, _ = make_llm(lambda req, body: httpx2.Response(status, json=err, headers={**headers, "request-id": "req_1"}))
    with pytest.raises(AIError) as e:
        llm.count_tokens(model="claude-opus-5-5", system=SYSTEM, messages=MSGS)
    assert (e.value.kind, e.value.status) == (kind, http)
    if status == 429:
        assert e.value.retry_after == 17 and e.value.extra["retry_after"] == 17
    assert "secret upstream detail" not in e.value.detail       # upstream text is logged, not shown to users


def test_errors_pickle_across_processes():
    import pickle

    e = pickle.loads(pickle.dumps(AIError("busy", 503, "busy", retry_after=9)))
    assert (e.kind, e.status, e.detail, e.retry_after) == ("busy", 503, "busy", 9)


def test_sdk_retries_429_then_succeeds():
    calls = {"n": 0}

    def responder(req, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "x"}},
                                   headers={"retry-after": "0"})
        return httpx2.Response(200, json={"input_tokens": 7})
    llm, _ = make_llm(responder, max_retries=2)
    assert llm.count_tokens(model="claude-opus-5-5", system=SYSTEM, messages=MSGS) == 7 and calls["n"] == 2


def test_connection_error():
    def boom(req, body):
        raise httpx2.ConnectError("down")
    llm, _ = make_llm(boom)
    with pytest.raises(AIError) as e:
        llm.count_tokens(model="claude-opus-5-5", system=SYSTEM, messages=MSGS)
    assert e.value.kind == "connection" and e.value.status == 503


def test_cancelling_closes_the_upstream_stream():
    """When the job is cancelled, ``on_text`` raises; leaving the SDK's stream context must close the HTTP response
    so the API stops generating (and billing) - verified at the transport level."""
    state = {"closed": False, "sent": 0}
    frames = stream_body(text_parts=tuple(f"word{i} " for i in range(200))).split(b"\n\n")

    class Body(httpx2.SyncByteStream):
        def __iter__(self):
            for f in frames:
                state["sent"] += 1
                yield f + b"\n\n"

        def close(self):
            state["closed"] = True

    llm, _ = make_llm(lambda req, b: httpx2.Response(200, headers=STREAM_HEADERS, stream=Body()))

    class Stop(Exception):
        pass

    got: list[str] = []

    def on_text(t):
        got.append(t)
        if len(got) >= 3:
            raise Stop

    with pytest.raises(Stop):
        llm.stream(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, max_tokens=100, effort="low", on_text=on_text)
    assert state["closed"] is True and state["sent"] < len(frames) // 2 and len(got) == 3


class ClosingBody(httpx2.SyncByteStream):
    """An SSE body that records how far it was read and whether the client closed it."""

    def __init__(self, frames: list[bytes]):
        self.frames, self.sent, self.closed = frames, 0, False

    def __iter__(self):
        for f in self.frames:
            self.sent += 1
            yield f + b"\n\n"

    def close(self):
        self.closed = True


def thinking_then_text(n_thinking: int, text: str = "Answer") -> list[bytes]:
    """A stream that spends ``n_thinking`` events thinking before any text (adaptive thinking)."""
    evs = [{"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                                  "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                                  "stop_sequence": None, "usage": {"input_tokens": 5,
                                                                                   "output_tokens": 1}}},
           {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "",
                                                                         "signature": ""}}]
    evs += [{"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "."}}
            for _ in range(n_thinking)]
    evs += [{"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 9}},
            {"type": "message_stop"}]
    return sse(*evs).split(b"\n\n")


class Stop(Exception):
    pass


def counting_cancel(after: int):
    calls = [0]

    def check():
        calls[0] += 1
        if calls[0] > after:
            raise Stop
    return check


def test_cancel_during_thinking_closes_the_stream_before_any_text():
    body = ClosingBody(thinking_then_text(300))
    llm, _ = make_llm(lambda req, b: httpx2.Response(200, headers=STREAM_HEADERS, stream=body))
    got: list[str] = []
    with pytest.raises(Stop):
        llm.stream(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, max_tokens=100, effort="low",
                   on_text=got.append, check_cancel=counting_cancel(10))
    assert body.closed and body.sent < 50 and got == []


def test_cancel_stops_a_structured_answer_mid_stream():
    body = ClosingBody(thinking_then_text(300, '{"notes": "x"}'))
    llm, _ = make_llm(lambda req, b: httpx2.Response(200, headers=STREAM_HEADERS, stream=body))
    with pytest.raises(Stop):
        llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={}, max_tokens=100,
                          effort="low", check_cancel=counting_cancel(20))
    assert body.closed and body.sent < 60


def test_cancelled_before_sending_makes_no_request():
    llm, rec = make_llm(lambda req, b: as_sse(message_json('{"notes": "x"}')))
    with pytest.raises(Stop):
        llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={}, max_tokens=10,
                          effort="low", check_cancel=counting_cancel(0))
    assert rec.requests == []


def test_cancelled_stream_is_booked_at_the_reservation(tmp_path):
    body = ClosingBody(thinking_then_text(300))
    llm, _ = make_llm(lambda req, b: httpx2.Response(200, headers=STREAM_HEADERS, stream=body))
    job = Ledger(tmp_path / "l.json", 10).job(1)
    with pytest.raises(Stop):
        metered(job, 0.25, load_prices(), "claude-opus-5-5",
                lambda: llm.stream(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, max_tokens=100,
                                   effort="low", on_text=lambda t: None, check_cancel=counting_cancel(5)),
                lambda f: f.usage)
    assert job.spent_usd == pytest.approx(0.25) and not job.open   # input + thinking so far may be billed


@pytest.mark.parametrize("body", [b"", b'{"not": "sse"}', sse({"type": "ping"})])
def test_stream_that_ends_without_a_message_is_an_upstream_error(body):
    llm, _ = make_llm(lambda req, b: httpx2.Response(200, headers=STREAM_HEADERS, content=body))
    with pytest.raises(AIError) as e:
        llm.complete_json(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={}, max_tokens=10,
                          effort="low")
    assert (e.value.kind, e.value.status) == ("upstream", 502) and not e.value.partial


def test_stream_cut_off_after_output_is_not_taken_as_a_complete_answer(tmp_path):
    full = stream_body(text_parts=("Half a sum",))
    body = full[:full.rindex(b"event: content_block_stop")]           # connection closed after the text delta
    llm, _ = make_llm(lambda req, b: httpx2.Response(200, headers=STREAM_HEADERS, content=body))
    job = Ledger(tmp_path / "l.json", 10).job(1)
    with pytest.raises(AIError) as e:
        metered(job, 0.2, load_prices(), "claude-opus-5-5",
                lambda: llm.stream(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, max_tokens=100,
                                   effort="low", on_text=lambda t: None), lambda f: f.usage)
    assert (e.value.kind, e.value.status, e.value.partial) == ("upstream", 502, True)
    assert job.spent_usd == pytest.approx(0.2)                          # output was produced: keep the worst case


def test_global_concurrency_is_bounded():
    active, peak, lock = [0], [0], threading.Lock()
    gate = threading.Event()

    def responder(req, body):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        gate.wait(0.2)
        with lock:
            active[0] -= 1
        return as_sse(message_json('{"notes": "x"}'))

    llm, _ = make_llm(responder, global_concurrency=2)
    threads = [threading.Thread(target=llm.complete_json, kwargs=dict(
        model="claude-opus-5-5", system=SYSTEM, messages=MSGS, schema={}, max_tokens=10, effort="low"))
        for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2


# ---- end to end through the SDK --------------------------------------------------------------------------------------
def test_summary_end_to_end_through_the_sdk(tmp_path):
    from app.ai.config import AISettings
    from app.ai.extract import PageText

    def responder(req, body):
        if req.url.path.endswith("/count_tokens"):
            return httpx2.Response(200, json={"input_tokens": 900})
        return httpx2.Response(200, headers=STREAM_HEADERS, content=stream_body(text_parts=("A ", "summary.")))

    llm, rec = make_llm(responder)
    s = AISettings.from_env({"ANTHROPIC_API_KEY": "sk-ant-test", "HEYP_STATE_DIR": str(tmp_path)})
    ledger = Ledger(s.ledger_path, s.daily_usd)
    job = ledger.job(s.request_usd)
    texts: list[str] = []
    res = summarize.run(llm, s, load_prices(), job, [[PageText(1, "Total visits rose to 412,000.", False)]],
                        summarize.SummaryOptions(), on_text=texts.append, on_progress=lambda f, m: None,
                        check_cancel=lambda: None)
    job.close()
    assert [r.url.path for r, _ in rec.requests] == ["/v1/messages/count_tokens", "/v1/messages"]
    assert texts == ["A ", "summary."] and res.text == "A summary." and res.served_by == "claude-opus-5-5"
    expected = (50 * 4 + 420 * 20 + 1200 * 5) / 1e6
    assert res.cost_usd == pytest.approx(expected, abs=1e-4)
    assert ledger.snapshot()["spent_usd"] == pytest.approx(expected, abs=1e-4)


def test_translation_end_to_end_through_the_sdk(tmp_path):
    import re as _re

    from app.ai import translate
    from app.ai.config import AISettings

    def responder(req, body):
        payload = body["messages"][0]["content"][-1]["text"]
        segs = json.loads(_re.search(r"<segments>\s*(.*?)\s*</segments>", payload, _re.S).group(1))
        out = {"translations": [{"id": x["id"], "text": x["text"].upper()} for x in segs]}
        return as_sse(message_json(json.dumps(out)))

    llm, rec = make_llm(responder)
    s = AISettings.from_env({"ANTHROPIC_API_KEY": "sk-ant-test", "HEYP_STATE_DIR": str(tmp_path)})
    units = [translate.Unit("u1", "Hello", 3), translate.Unit("u2", "Total amount", 4)]
    job = Ledger(s.ledger_path, s.daily_usd).job(1)
    report = translate.TranslateReport()
    results = translate.translate_units(llm, s, load_prices(), job, units, "de", None, report,
                                        on_progress=lambda f, m: None, check_cancel=lambda: None)
    job.close()
    _, body = rec.requests[0]
    assert results == {"u1": "HELLO", "u2": "TOTAL AMOUNT"} and report.calls == 1
    assert body["output_config"]["format"]["schema"] == translate.SCHEMA
    assert body["output_config"]["effort"] == "low" and body["system"][0]["text"].startswith("You are the translation")


def test_stream_error_after_output_is_booked_at_the_reservation(tmp_path):
    full = stream_body(text_parts=("Partial ",))
    body = full[:full.rindex(b"event: content_block_stop")] + sse(        # cut after the text delta
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    llm, _ = make_llm(lambda req, b: httpx2.Response(200, headers=STREAM_HEADERS, content=body))
    job = Ledger(tmp_path / "l.json", 10).job(1)
    with pytest.raises(AIError) as e:
        metered(job, 0.3, load_prices(), "claude-opus-5-5",
                lambda: llm.stream(model="claude-opus-5-5", system=SYSTEM, messages=MSGS, max_tokens=100,
                                   effort="low", on_text=lambda t: None), lambda f: f.usage)
    assert e.value.kind == "busy" and e.value.partial is True
    assert job.spent_usd == pytest.approx(0.3)            # partial output was billed: keep the worst case
