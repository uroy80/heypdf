<!--
  heypdf — PRIVACY POLICY (hosted mode). Rendered by app/legal.py at /privacy.

  Syntax: a Mustache subset. {{name}} = value (HTML-escaped), {{#flag}}…{{/flag}} = shown when the flag is set,
  {{^flag}}…{{/flag}} = shown when it is not, {{> glance}} = legal/glance.md. HTML comments (like this one and every
  "VERIFY:" note) are removed BEFORE rendering, so they may mention placeholders freely and never reach visitors.
  Every value comes from a HEYP_* setting (see deploy/heypdf.env.example and app/legal.py `values()`).

  Rule for editors: every sentence must be literally true for the code and the deployment files in this repository
  (deploy/compose.prod.yml, deploy/apache/). If you change the system, change this page in the same pull request;
  tests/test_legal.py checks the parts that can be checked mechanically.
-->

# Privacy

*Effective {{effective_date}} · applies to {{site_name}} at {{public_host}}*

{{> glance}}

## The short version

{{site_name}} is a free toolbox for PDFs and documents. You don't need an account and we don't want one from you. To
do the work, your files have to travel to our server{{#cdn_provider}} through {{cdn_provider}}, the network that
delivers our site{{/cdn_provider}}; they're processed there and deleted automatically — at the latest
{{ttl_minutes}} minutes after you last used them{{#hard_ttl_minutes}}, and never more than {{hard_ttl_minutes}}
minutes after you uploaded them{{/hard_ttl_minutes}}. We don't sell anything, show ads, run analytics or set cookies.
{{#ai_enabled}}{{#cdn_provider}}Apart from that delivery network, the{{/cdn_provider}}{{^cdn_provider}}The{{/cdn_provider}}
only time what's in your files goes to another company is when you choose one of the AI tools — then the text of that
document goes to Anthropic, the company behind Claude, to do the job. {{/ai_enabled}}The software is open source, so
you can check this page against the code.

## Who runs this

{{site_name}} is run by **{{operator_name}}**, based in {{operator_country}} ("we", "us"). For anything about
privacy, write to **{{contact_email}}**.{{#postal_address}} Post: {{postal_address}}.{{/postal_address}}

{{#eu_representative}}Our representative in the European Union is {{eu_representative}}. {{/eu_representative}}{{#uk_representative}}Our representative in the United Kingdom is {{uk_representative}}. {{/uk_representative}}We
haven't appointed a data protection officer; the law doesn't require one for a service like this. The person who
answers privacy questions is {{grievance_officer_name}}, at {{contact_email}}.
<!-- VERIFY: GDPR Art. 27 / UK GDPR Art. 27 representative (HEYP_EU_REPRESENTATIVE / HEYP_UK_REPRESENTATIVE).
     A public website is not "occasional" processing, so strictly a representative is required — decide with a lawyer. -->

## What happens to your files

1. **Upload.** When you add a file, your browser sends it to our server over an encrypted (HTTPS)
   connection{{#cdn_provider}}, through {{cdn_provider}}, which delivers our site{{/cdn_provider}}. It's
   kept in temporary storage that lives in the server's memory, under a random code — not in a database and not in
   any backup. Next to it we keep a few facts the tools need to show you, such as its original name, size and page
   count.
2. **Processing.** Our server does what you asked — merging, splitting, compressing, converting, signing, redacting
   and so on.{{#docs_enabled}} Office conversions, compression, repair, PDF/A and text recognition (OCR) run in a
   separate, locked-down part of the server that has no internet access at all.{{/docs_enabled}}{{#ai_enabled}} The
   AI tools are the one exception to "nothing leaves our server", explained below.{{/ai_enabled}}
3. **Results.** What a tool makes is kept the same way as your uploads, so that you can preview it, download it
   or continue with another tool. It follows the same deletion rules.
4. **Deletion.** Your files, and everything made from them, are deleted:
   - as soon as you remove them from the page;
   - when you close the tab — the page asks our server to delete them straight away. If that message can't get
     through (say your connection drops), the next rule catches them;
   - **{{ttl_minutes}} minutes after you last used them**, at the latest{{#hard_ttl_minutes}}, and never more than
     {{hard_ttl_minutes}} minutes after you uploaded them, however busy you keep them{{/hard_ttl_minutes}};
   - whenever the server restarts.
5. **Passwords and certificates.** If you unlock or protect a PDF, or sign one with your own certificate file, the
   password and the certificate are used for that one job only. They're never written to our logs and are deleted
   as soon as the job ends.
6. **Phone scanning.** If you use "Use your phone" to scan pages, the QR code holds a one-off link that stops
   working after 15 minutes. Photos sent from your phone are stored and deleted exactly like other uploads.

Nobody looks at your files. We don't copy them, index them, scan them for advertising, or use them to train any AI
model — ours or anyone else's.

**Photos in your PDFs.** A plain JPEG photo you turn into a PDF is placed in it exactly as it is, including details
your camera may have stored in it, such as where it was taken. Other images, and pages you scan, are re-encoded, which
leaves those details out.

**Previews in your browser.** To keep things quick, your browser shows previews of your files and pages.
{{#preview_no_store}}We tell your browser not to keep them after you leave the page.{{/preview_no_store}}{{^preview_no_store}}Your browser may keep those previews in its own cache on your device for up to an hour.{{/preview_no_store}}
<!-- VERIFY (implementation): hosted mode sends "Cache-Control: no-store" on every API response — files, page images,
     jobs (app/main.py, PLAN.md §2.7); the idle TTL (HEYP_TTL_MINUTES) and the hard TTL (HEYP_HARD_TTL_MINUTES) are
     both enforced and results inherit their inputs' hard deadline; .p12 files and passwords are job-scoped. -->
<!-- VERIFY (deployment): the upload storage is a tmpfs (RAM). Pages can still reach disk if the VPS has swap —
     run `swapon --show`; disable swap or use encrypted swap, or reword the "memory" sentence. -->

{{#ai_enabled}}
<a id="ai"></a>

## The AI tools

Some {{site_name}} tools use Claude, an AI model made by **Anthropic**: {{ai_tools}}. They only run when you start
one yourself, and the first time you do, we tell you what will happen before anything is sent.

- **What's sent:** the text we extract from your document, plus our instructions for the task (for example
  "summarise this" or "translate this into French"). We don't send your file name, your IP address or anything
  else that identifies you, and we never send the file itself.
- **Who receives it:** Anthropic, through its API, as our service provider. Anthropic processes it under its
  [Commercial Terms of Service](https://www.anthropic.com/legal/commercial-terms) and
  [Data Processing Addendum](https://www.anthropic.com/legal/data-processing-addendum).
- **Training:** Anthropic's commercial terms say it may not train its models on content sent through its API.
- **How long Anthropic keeps it:** {{#ai_zdr}}we have a zero-data-retention arrangement with Anthropic, so it doesn't
  store the text or the result once it has answered — except that requests its automated safety systems flag can
  be kept for up to 2 years, and data can be kept where the law requires.{{/ai_zdr}}{{^ai_zdr}}Anthropic's published
  policy is to delete API inputs and outputs within 30 days. It can keep them longer — up to 2 years — if its
  automated safety systems flag a request as breaking its [Usage Policy](https://www.anthropic.com/legal/aup), or
  where the law requires.{{/ai_zdr}}
- **Where:** Anthropic stores API data in the United States. {{#ai_geo_us}}We ask Anthropic to run these requests only
  on infrastructure in the United States.{{/ai_geo_us}}{{^ai_geo_us}}The model itself may run in other countries
  where Anthropic operates.{{/ai_geo_us}}
- **What we keep:** the result is stored with your other files and deleted the same way. To keep the cost of the
  AI tools under control we record how much each request cost and when — never the text, the result, your file
  names or your IP address.
- **Accuracy:** AI output can be wrong, incomplete or out of date. Don't rely on facts in a summary or translation
  without checking them against the original — and please don't treat it as legal, medical or financial advice.
  Results are labelled as AI-generated.

If you'd rather nothing left our server, simply don't use the AI tools — every other tool works without them.
<!-- VERIFY (implementation, app/ai/**): (1) an AI request is only made after an explicit click, with a first-use
     notice linking to /privacy#ai; (2) the request body holds only extracted text and the task prompt — no file
     name, no metadata.user_id, no client IP; (3) inference_geo comes from HEYP_AI_INFERENCE_GEO; (4) the spend ledger
     in HEYP_STATE_DIR stores amounts and times only (no client key); (5) outputs carry the AI-generated label. -->
<!-- VERIFY: Anthropic facts last checked 17 October 2026 against the Commercial Terms (effective 17 Jun 2025, §B,
     §D.3), privacy.claude.com article 7996866 (updated 1 Jul 2026) and the platform data-residency page.
     Re-check at least every 6 months and whenever Anthropic announces a terms change. -->
{{/ai_enabled}}

{{#url_tool_enabled}}
<a id="web-capture"></a>

## Turning a web page into a PDF

If you give us a web address to turn into a PDF, our server visits that page for you with a separate, locked-down
browser that can only reach the public internet. The website you named sees a visit from our server, not from you,
and its name is looked up through our server's ordinary internet name service (DNS). The address travels inside the
request, not in the page address, so it never appears in our access log, and we don't keep it after the job. To stop
the tool being used against other websites, we count visits per website for a few minutes, in memory only.
<!-- VERIFY (implementation): the URL is sent in the POST body (POST /api/jobs options), never in a query string,
     and is never logged; the per-destination-host counter lives in memory (app/core/limits.py). -->
{{/url_tool_enabled}}

## What our server records

Like almost every website, the web server in front of {{site_name}} keeps an **access log**. For each request it
records:

- {{#log_ip_truncated}}a shortened version of your IP address (the last part is removed, so it points to a group of
  addresses rather than to you){{/log_ip_truncated}}{{^log_ip_truncated}}your IP address{{/log_ip_truncated}};
- the date and time;
- which part of the site was asked for (for example `/api/jobs`) and the random code of a file — never the file's
  name or contents, and never anything after a "?" in the address;
- whether it worked, how big the response was, and how long it took.

We don't log your browser type, the page you came from, or anything inside your files. The {{site_name}} app itself
keeps no access log of its own here; its error messages never include file names or contents.{{#cdn_provider}}
{{cdn_provider}}, which carries every request to our server, also sees each request on the way — including your IP
address and browser type — and processes it under its data processing terms (see "Who else is involved").{{/cdn_provider}}
<!-- VERIFY (deployment): true only with the log format in deploy/apache/ (an Apache vhost without its own CustomLog
     writes the "combined" format with user agent, referrer and query string; behind Cloudflare the IP is the
     visitor's only with mod_remoteip as shipped). tests/test_legal.py checks the shipped vhosts. -->

We use these logs to keep the service running and safe: spotting attacks and abuse, fixing errors and
understanding load. They're deleted after **{{log_retention_days}} days**.{{#backup_retention_days}} Our hosting
provider makes automatic backups of the server's disk, and a log can survive in those backups for up to
{{backup_retention_days}} more days before the backup itself is deleted. Your files are never in those backups —
they only ever exist in memory.{{/backup_retention_days}}
<!-- VERIFY: Hostinger VPS automatic weekly backups are on by default and keep up to 2 weekly copies (about 14 days).
     Set HEYP_BACKUP_RETENTION_DAYS accordingly (0 = backups off). -->

{{#rate_limit_enabled}}To stop any one visitor from overloading the service, we count requests{{^ai_enabled}} and uploads{{/ai_enabled}}{{#ai_enabled}}, uploads and AI
use{{/ai_enabled}} per visitor. The counters are keyed by a scrambled form of your IP address that changes every day and can't be
turned back into the address; they live only in the server's memory and are gone within a day.
{{/rate_limit_enabled}}

## Cookies and storage on your device

**We don't use cookies.** There's no analytics, no advertising, no social-media buttons, no tracking pixels, and no
fonts or scripts loaded from other companies — everything comes from our own server.

We store something on your device only when you ask for it, in your browser's storage — never in a cookie,
and it never leaves your device:

- when you move files from one tool to a page that has to load afresh (for example the scanner), the tab briefly
  keeps the list of files being handed over (their names and random codes) in session storage, under the name
  `heypdf.handoff`; the next page reads and deletes it straight away, and it's gone when you close the tab;
- when you press the light/dark theme button, your browser remembers that choice, under the name
  `heypdf-theme`{{#ai_enabled}};{{/ai_enabled}}{{^ai_enabled}}.{{/ai_enabled}}
{{#ai_enabled}}- when you confirm the notice that appears before your first AI request, it remembers that you've seen it,
  under the name `heypdf-ai-notice`, so the notice doesn't interrupt you every time.
{{/ai_enabled}}

You can clear these with your browser's "clear site data" setting at any time.
<button type="button" class="legal-forget" data-forget-theme hidden>Forget my choices on this device</button>
<!-- VERIFY (implementation): static/js/theme.js un-hides [data-forget-theme] and removes the keys above on click;
     without that script the button stays hidden and the sentence above is still true. -->
<!-- VERIFY: tests/test_legal.py fails when the UI writes a browser-storage key that this section doesn't name. -->
<!-- VERIFY (deployment, CDN): Cloudflare features set cookies (__cf_bm: Bot Fight Mode / bot management;
     cf_clearance: challenges, "I'm Under Attack") or inject scripts (/cdn-cgi/: Email Address Obfuscation, Rocket
     Loader, Web Analytics auto-setup, Zaraz). They must stay off for this host name (deploy/DEPLOY.md §9); the
     Deploy workflow checks the public pages for Set-Cookie and /cdn-cgi/ after every deploy. -->

Because we don't track you, there's nothing for "Do Not Track" or Global Privacy Control signals to switch off —
but you're welcome to send them.

## Who else is involved

We don't sell or share your personal data. A few companies help us run the service and can technically come into
contact with it:

| Who | What they do | What they can see |
|---|---|---|
| {{hosting_provider}} | Hosts our server{{#server_location}} in {{server_location}}{{/server_location}} | Everything on the server, as any host can; they process it only on our instructions, under their data processing terms |
{{#cdn_provider}}| {{cdn_provider}} | Delivers and protects our website: the encrypted connection to your browser, the network, and filtering out attacks | Everything you send or receive while it travels — your files, the results, your IP address and browser details; processed under its {{#cdn_dpa_url}}[data processing terms]({{cdn_dpa_url}}){{/cdn_dpa_url}}{{^cdn_dpa_url}}data processing terms{{/cdn_dpa_url}} |
{{/cdn_provider}}{{#ai_enabled}}| {{anthropic_entity}} | Runs the AI tools | Only the text you send through an AI tool (see above) |
{{/ai_enabled}}

We'd only hand anything else over if the law makes us — and since files are deleted within hours at most, there's
usually nothing to hand over.

## Where your data goes

Our server is {{#server_location}}in **{{server_location}}**{{/server_location}}{{^server_location}}run by {{hosting_provider}}{{/server_location}}. We look after it remotely from
{{operator_country}}.{{#cdn_provider}} On the way between you and our server, everything passes through {{cdn_provider}}'s
worldwide network — normally its data centre nearest to you, which may be in another country.{{/cdn_provider}}{{#ai_enabled}}
Text you send through an AI tool goes to Anthropic in the United States{{^ai_geo_us}}
(and may be processed in other countries where Anthropic runs its models){{/ai_geo_us}}.{{/ai_enabled}}

Where European or UK data protection law applies to these transfers, we rely on the protections built into our
providers' contracts: the European Commission's Standard Contractual Clauses, which are part of the data processing
terms of {{transfer_providers}}{{#ai_enabled}} (Anthropic's also include the UK Addendum){{/ai_enabled}}.
<!-- VERIFY: Hostinger DPA (rev. 29 Sep 2026, §9) deems SCCs Modules 2/3 (Decision 2021/914) signed on accepting its
     ToS; Anthropic DPA (eff. 24 Feb 2025) has SCC Modules 2/3, UK Addendum B.1.0 and a Swiss addendum. Check
     Hostinger's UK coverage before claiming it for the UK. Cloudflare Customer DPA
     (cloudflare.com/cloudflare-customer-dpa, checked 18 Oct 2026) incorporates the SCCs and the UK Addendum; it is
     part of Cloudflare's self-serve terms. Another CDN in HEYP_CDN_PROVIDER: check its DPA has SCCs first. -->

## Why we're allowed to do this (legal bases)

If you're in the EU, EEA or UK, the law asks us to tell you the legal reason for each use of your data:

| What | Why | Legal basis |
|---|---|---|
| Processing your files with the tool you chose{{#ai_enabled}}, including sending text to Anthropic when you start an AI tool{{/ai_enabled}} | To give you the result you asked for | Necessary to provide the service you request (GDPR Art. 6(1)(b)); for other people who appear in your files, our and your legitimate interest in getting your task done (Art. 6(1)(f)) |
| Access logs{{#rate_limit_enabled}} and abuse limits{{/rate_limit_enabled}} | Security, preventing abuse, fixing faults | Our legitimate interest in keeping the service secure and working (Art. 6(1)(f); see Recital 49) |
| Replying when you write to us | To answer you | Our legitimate interest in answering, or a legal obligation |

If you're in India: you provide your files voluntarily so that we can do a specific job with them, which the
Digital Personal Data Protection Act, 2023 treats as a "legitimate use" (section 7(a)). We use them for nothing else.

**Who's responsible for what.** For our access logs, and for running the tools for people using them in their own
lives, we're responsible for the data (in GDPR terms, the "controller"). If you use the tools for an
organisation's work — say, merging staff forms or scanning patients' letters — your organisation is responsible for
that data and we process it only on your instructions, as a "processor"; our [Terms](/terms#your-files) contain the
promises the law requires from us in that role. Either way the rules on this page are the same: your files are used
for your task and nothing else, and they're deleted automatically.

## How long we keep things

| Data | How long |
|---|---|
| Your uploaded files, file names and everything made from them | Until you remove them or close the tab, and never more than {{ttl_minutes}} minutes after you last used them{{#hard_ttl_minutes}} or {{hard_ttl_minutes}} minutes after upload{{/hard_ttl_minutes}} |
| PDF passwords and signing certificates | Only for the job that uses them |
{{#ai_enabled}}| Text sent to an AI tool | Not kept by us after the job; at Anthropic, {{#ai_zdr}}not stored once answered{{/ai_zdr}}{{^ai_zdr}}up to 30 days{{/ai_zdr}} (longer only if flagged for safety review, up to 2 years, or if the law requires) |
| AI cost records | Amounts and times only, no personal data; kept to enforce daily spending limits |
{{/ai_enabled}}| Access logs | {{log_retention_days}} days{{#backup_retention_days}} (plus up to {{backup_retention_days}} days in server backups){{/backup_retention_days}} |
{{#rate_limit_enabled}}| Abuse-limit counters | In memory, gone within a day |
{{/rate_limit_enabled}}| Theme choice | On your device, until you clear it |
| Emails you send us | As long as needed to deal with your message, then deleted — normally within 12 months |

## Your rights

Depending on where you live, you can ask us to tell you what data we hold about you, give you a copy, correct it,
delete it, restrict how we use it, or object to our using it. You can also complain to us first, and you always
have the right to complain to a regulator.

To be honest about what that means here: we hold almost nothing that's linked to you. Your files are already gone,
usually within the hour, and we have no account, name or email for you. Access logs contain IP addresses, so if you
send us your IP address and the dates you used the site, we'll find and delete — or send you — the matching lines.

Write to **{{contact_email}}**. We'll acknowledge your message within a day or two and give you a full answer within
one month at the latest. We may need to ask a question to make sure we're dealing with the right person. There's no
charge.

Regulators you can contact:

- **European Union / EEA:** the data protection authority in your country — list at
  [edpb.europa.eu](https://www.edpb.europa.eu/about-edpb/about-edpb/members_en).
- **United Kingdom:** the Information Commissioner's Office — [ico.org.uk/make-a-complaint](https://ico.org.uk/make-a-complaint/).
- **India:** the Data Protection Board of India, once you've used our grievance process below.
- **Elsewhere:** your local data protection or consumer authority.

## Grievance officer (India)

Our grievance officer is **{{grievance_officer_name}}**, reachable at {{contact_email}}. We acknowledge complaints
within 24 hours and resolve them within {{grievance_days}} days.
<!-- VERIFY: IT (Intermediary Guidelines…) Rules 2021 r.3(2) as amended Feb 2026 — 24 h acknowledgement, 7-day
     resolution (was 15). -->

## Children

{{site_name}} is a general-purpose tool for adults. If you're under 18, please use it with a parent or guardian. We
don't knowingly collect anything from children, and since we don't keep files, there's nothing to build a profile
from.
<!-- VERIFY: India's DPDP s.9 + Rule 10 (verifiable parental consent for under-18s) apply from about 13 May 2027 —
     get advice before then. -->

## Security

Connections are encrypted with HTTPS{{#cdn_provider}} — between you and {{cdn_provider}}, and again between
{{cdn_provider}} and our server{{/cdn_provider}}. Each part of the service runs in its own isolated container with a read-only
file system, and uploads live only in temporary, memory-backed storage that's wiped on restart.{{#docs_enabled}} The
part that converts Office files and runs OCR has no network access at all.{{/docs_enabled}}{{#url_tool_enabled}} The
browser that turns web pages into PDFs is walled off from the rest of the server and can reach only public websites,
through a filter that blocks private addresses.{{/url_tool_enabled}} Our server makes no other outbound connections{{#ai_enabled}}
except to Anthropic for the AI tools{{/ai_enabled}}. No system is perfectly secure, but there isn't much to steal:
we don't keep your files for long, and we don't have accounts or passwords.
<!-- VERIFY (deployment, CDN): "again between Cloudflare and our server" holds only with the zone's SSL/TLS mode on
     Full (strict) and the Origin certificate on Apache (deploy/DEPLOY.md §9); "Flexible" would be plain HTTP. -->

If we ever discover a breach that affects you and we have a way to reach you, we'll tell you, and we'll tell the
regulators the law requires us to tell.

Found a security problem? Please write to {{security_email}} or use the private reporting link in our
[security policy]({{source_url}}/security/policy).

## Check our work

{{site_name}} is free software under the GNU Affero General Public License v3.0. The exact code running on this site
— version {{version}}{{#source_revision}}, commit `{{source_revision}}`{{/source_revision}} — is at
[{{source_tree_url}}]({{source_tree_url}}). If something on this page doesn't match what the code does, the code is
the bug: please tell us.

## Changes to this page

When we change how we handle data, we'll update this page and the date at the top. If a change is significant — say,
a new service provider that sees your files — we'll also flag it on the site for at least 30 days. Earlier versions
are in the project's history on GitHub.
