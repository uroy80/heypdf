<!--
  Shown at /privacy and /terms when HEYP_MODE=local (the default): a copy of heypdf that someone runs for themselves.
  The hosted-mode policy (privacy.md / terms.md) would be untrue here — no operator details are configured — so this
  page explains only what the software itself does, from this copy's own settings. Same syntax and values as
  privacy.md.
-->

# {{page_title}}

*{{site_name}} · version {{version}} · not configured as a public service*

This copy of {{site_name}} runs in local mode: whoever started it hasn't set it up as a public website, so there's no
separate privacy policy or terms of use. The person who runs it is responsible for it, and the software works as
described below.

## What the software does with your files

- Files are uploaded to the server running this copy of {{site_name}}. They're kept in temporary storage and deleted
  when you remove them or close the tab, and at the latest {{ttl_minutes}} minutes after you last used
  them{{#hard_ttl_minutes}} or {{hard_ttl_minutes}} minutes after upload{{/hard_ttl_minutes}}, and whenever it restarts.
- It sets no cookies and runs no analytics. Your browser remembers only your light/dark theme choice, under the name
  `heypdf-theme`, and only if you press the theme button{{#ai_enabled}} — and, once you've confirmed the AI notice,
  that you've seen it (`heypdf-ai-notice`){{/ai_enabled}}. When files move from one tool to a page that loads afresh
  (the scanner), the tab keeps their list for a moment in session storage (`heypdf.handoff`) and deletes it on
  arrival.
- The software doesn't send your files to anyone else{{#ai_enabled}}, except when you start one of the AI tools
  ({{ai_tools}}): then the text extracted from that document goes to Anthropic's Claude API, using the API key of the
  person who runs this copy. Anthropic doesn't train on it and normally deletes it within 30 days. AI output can be
  wrong — check important facts against the original{{/ai_enabled}}.{{#url_tool_enabled}} The HTML to PDF tool visits
  the web addresses you give it, through a locked-down browser that can reach only public websites.{{/url_tool_enabled}}

## Licence

{{site_name}} is free software under the GNU Affero General Public License v3.0. Source code:
[{{source_tree_url}}]({{source_tree_url}}). Third-party fonts and programs keep their own licences, listed on the
[credits page](/credits).

If you run {{site_name}} as a public website, set `HEYP_MODE=hosted` and the operator settings described in
`deploy/DEPLOY.md`: this page is then replaced by a full privacy policy and terms of use.
