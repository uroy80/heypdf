"""app.core.registry: validation, lookup, availability and the /api/tools JSON."""
from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.core import registry
from app.core.errors import ToolError
from app.core.registry import Tool


class Opts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: str = Field("all", json_schema_extra={"ui": {"widget": "text"}})
    mode: Literal["a", "b"] = "a"


class LooseOpts(BaseModel):
    pages: str = "all"


def run(ctx, options) -> None:
    pass


def other_run(ctx, options) -> None:
    pass


def make(**kw) -> Tool:
    base = dict(id="pdf.zz_reg_test", title="Registry test", summary="One line.", category="Organize",
                inputs=("pdf",), options=Opts, run=run)
    return Tool(**{**base, **kw})


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for tool_id in ("pdf.zz_reg_test", "pdf.zz_reg_other", "pdf.zz_reg_docs"):
        registry.unregister(tool_id)
    registry.clear_engine_cache()


def test_register_get_and_slug():
    tool = registry.register(make(inputs=(".PDF", "jpg")))
    assert registry.get("pdf.zz_reg_test") is tool
    assert tool.slug == "zz-reg-test" and tool.inputs == ("pdf", "jpeg")
    assert tool in registry.all_tools()
    with pytest.raises(KeyError):
        registry.get("pdf.nope")
    with pytest.raises(TypeError):
        tool.limits["max_pages"] = 3  # frozen


@pytest.mark.parametrize(("change", "message"), [
    ({"id": "merge"}, "id must look like"),
    ({"id": "PDF.Merge"}, "id must look like"),
    ({"id": "img.make_gif"}, "id must look like"),
    ({"category": "Misc"}, "category must be one of"),
    ({"options": LooseOpts}, "extra='forbid'"),
    ({"options": dict}, "pydantic BaseModel"),
    ({"inputs": ()}, "inputs"),
    ({"min_files": 3, "max_files": 2}, "min_files"),
    ({"min_files": 0, "max_files": 0}, "min_files"),
    ({"min_files": -1}, "min_files"),
    ({"lane": "turbo"}, "lane"),
    ({"engine": "gpu"}, "engine"),
    ({"timeout_s": 0}, "timeout_s"),
    ({"limits": {"max_widgets": 3}}, "unknown limit"),
    ({"limits": {"max_pages": -1}}, "non-negative"),
    ({"title": " "}, "required"),
])
def test_invalid_tools_are_refused(change, message):
    with pytest.raises(ValueError, match=message):
        registry.register(make(**change))


def test_tools_may_take_no_input_file():
    """URL captures (renderer) run from options alone: min_files=0, at most one optional file."""
    tool = registry.register(make(min_files=0, max_files=1))
    assert tool.min_files == 0
    assert registry.tool_json(tool, Settings())["min_files"] == 0


def test_duplicates():
    registry.register(make())
    registry.register(make())  # the same definition again (module reload) is fine
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make(run=other_run))
    assert registry.register(make(id="pdf.zz_reg_other", run=other_run, title="Other")).slug == "zz-reg-other"


def test_options_parsing_and_schema():
    tool = registry.register(make())
    assert registry.parse_options(tool, None) == Opts()
    assert registry.parse_options(tool, {"mode": "b"}).mode == "b"
    with pytest.raises(ToolError) as info:
        registry.parse_options(tool, {"mode": "c"})
    assert info.value.status == 422 and info.value.detail.startswith("options.mode:")
    with pytest.raises(ToolError, match="not permitted"):
        registry.parse_options(tool, {"surprise": 1})
    with pytest.raises(ToolError, match="must be an object"):
        registry.parse_options(tool, [1, 2])
    schema = registry.options_schema(tool)
    assert schema["properties"]["pages"]["ui"] == {"widget": "text"}


def test_accepts_kinds_and_formats():
    tool = registry.register(make(inputs=("pdf", "docx")))
    assert registry.accepts(tool, "pdf", "pdf")
    assert registry.accepts(tool, "office", "docx")
    assert not registry.accepts(tool, "office", "xlsx")
    assert not registry.accepts(tool, "other", "docx")  # an unknown file named .docx is never accepted
    assert registry.describe_inputs(tool) == "PDF files or .docx"


def test_engine_availability(monkeypatch):
    settings = Settings()
    docs_tool = registry.register(make(id="pdf.zz_reg_docs", engine="docs", lane="docs", run=other_run,
                                       title="Docs"))
    monkeypatch.setattr(registry, "_engine_checks", {})
    ok, reason = registry.availability(docs_tool, settings)
    assert not ok and "document service" in reason
    calls = []

    def healthy():
        calls.append(1)
        return True, ""

    registry.register_engine_check("docs", healthy)
    assert registry.availability(docs_tool, settings) == (True, "")
    assert registry.availability(docs_tool, settings) == (True, "")
    assert len(calls) == 1  # network health answers are cached

    def broken():
        raise ConnectionError("down")

    registry.register_engine_check("docs", broken)
    ok, reason = registry.availability(docs_tool, settings)
    assert not ok and "not responding" in reason
    with pytest.raises(ValueError):
        registry.register_engine_check("gpu", healthy)


def test_ai_engine_without_a_registered_check_follows_the_key(monkeypatch):
    monkeypatch.setattr(registry, "_engine_checks", {})
    assert registry.engine_status("ai", Settings(ai_key_set=False))[0] is False
    assert registry.engine_status("ai", Settings(ai_key_set=True)) == (True, "")


def test_tool_check_and_json():
    tool = registry.register(make(check=lambda: (False, "Library missing"), limits={"max_pages": 50}))
    body = registry.tool_json(tool, Settings())
    assert body["available"] is False and body["reason"] == "Library missing"
    assert body["slug"] == "zz-reg-test" and body["limits"] == {"max_pages": 50}
    assert {"id", "title", "summary", "category", "slug", "inputs", "min_files", "max_files",
            "options_schema", "limits", "lane", "available", "reason"} <= set(body)
