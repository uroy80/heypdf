`heypdf-tools.json` is a snapshot of the real tool registry (ids, inputs, file counts, limits and the pydantic
`options_schema` of every tool) so the UI tests exercise the schemas the server actually sends.
Regenerate it after changing an options model:

```sh
docker run --rm -e PYTHONPATH=/app -v "$PWD":/app:ro -w /app heypdf:dev python -c '
import json, sys, app.tools
from app.core import registry
app.tools.load()
out = [{"id": t.id, "title": t.title, "summary": t.summary, "category": t.category, "slug": t.slug,
        "inputs": list(t.inputs), "min_files": t.min_files, "max_files": t.max_files, "limits": dict(t.limits),
        "lane": t.lane, "engine": t.engine, "options_schema": registry.options_schema(t)} for t in registry.all_tools()]
json.dump({"generated_from": "app.core.registry", "tools": out}, sys.stdout, ensure_ascii=False, indent=1)
' > static/tests/fixtures/heypdf-tools.json
```
