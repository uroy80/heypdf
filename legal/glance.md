<!--
  "Privacy at a glance" — the boxed fact box at the top of /privacy (included with {{> glance}}).
  Same syntax and values as privacy.md. Keep it to a handful of lines; every line must also be true.
-->
<aside class="factbox" aria-labelledby="glance-title">

<h2 id="glance-title" class="factbox-title">Privacy at a glance</h2>

- **No account, no cookies, no analytics, no ads.** Nothing tracks you here.
- **Your files are processed on our server, then deleted** — when you remove them or close the tab, and never more
  than **{{ttl_minutes}} minutes** after you last used them{{#hard_ttl_minutes}} (**{{hard_ttl_minutes}} minutes** after
  upload at most){{/hard_ttl_minutes}}.
- **Nobody looks at your files**, and they're never used to train AI.
{{#ai_enabled}}- **AI tools are the one exception, and only when you start them:** the document's text goes to Anthropic
  (Claude) to do the job. Anthropic doesn't train on it and {{#ai_zdr}}doesn't store it once it has answered{{/ai_zdr}}{{^ai_zdr}}normally deletes it within 30 days{{/ai_zdr}}.
{{/ai_enabled}}- **Our server keeps a basic access log** ({{#log_ip_truncated}}shortened {{/log_ip_truncated}}IP address,
  time, page) for {{log_retention_days}} days, to keep the service safe. Never your file names or contents.
- **Open source** (AGPL-3.0) — [read the code]({{source_tree_url}}) behind this page.

Questions: {{contact_email}} · [Full privacy policy](/privacy) · [Terms](/terms)

</aside>
