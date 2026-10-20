<!--
  heypdf — TERMS OF USE (hosted mode). Rendered by app/legal.py at /terms. Same syntax and values as
  privacy.md; HTML comments ("VERIFY:" notes) are removed before rendering. Written from public sources, not legal
  advice: have a lawyer look at §3 (processor terms), §4 (intermediary notice), §8 (liability) and §11 (law).
-->

# Terms of use

*Effective {{effective_date}} · applies to {{site_name}} at {{public_host}}*

These are the rules for using {{site_name}} ("the service"), a free toolbox for PDFs and documents. They're short, and
we've tried to write them so you'll actually read them. By using the service you agree to them; if you don't, please don't
use it. Our [privacy policy](/privacy) explains what happens to your data and is part of these terms.

## 1. Who we are

The service is run by **{{operator_name}}**, based in {{operator_country}} ("we", "us"). Contact:
**{{contact_email}}**.{{#postal_address}} Post: {{postal_address}}.{{/postal_address}}

## 2. The service

- It's **free**, and there's no account. You can use it as much as you like within the limits shown on the site —
  for example, files up to {{max_upload_mb}} MB{{#rate_limit_enabled}}, and a fair-use number of jobs{{^ai_enabled}} and
  uploads{{/ai_enabled}}{{#ai_enabled}}, uploads and AI requests{{/ai_enabled}} per visitor each hour or day{{/rate_limit_enabled}}.
- It's provided **as it is, when it's available.** We may change tools, limits or features, take the service down
  for maintenance, or stop it altogether, without notice. The code stays available under its open-source licence
  (section 9), so you can always run it yourself.
- We may slow down or block traffic — including from a particular IP address — that is abusing the service or
  putting it at risk for everyone else.
- Some things we deliberately don't offer: we don't send documents to other people for signature, we don't store
  your documents for later, and we can't open a PDF whose password you don't know.

<a id="your-files"></a>

## 3. Your files

**They're yours.** You keep every right you have in the files you upload and in the results. We claim nothing. You
give us permission to store, process and transmit them only as far as needed to carry out the tool you chose and
give you the result — and that permission ends when the files are deleted.

**How we treat them.** We process your files only to do what you asked, and we delete them automatically, as
described in the [privacy policy](/privacy): when you remove them or close the tab, and never more than
{{ttl_minutes}} minutes after you last used them{{#hard_ttl_minutes}} or {{hard_ttl_minutes}} minutes after you
uploaded them{{/hard_ttl_minutes}}. Don't use the service as storage: **keep your originals**, because once a file is
deleted we can't get it back.

**If you're processing other people's data for an organisation.** When you use the service for an organisation's
work and your files contain personal data you're responsible for, we act as your processor, and we promise to:

- process that data only to carry out your instructions — the tool you choose and the settings you pick;
- make sure that anyone with access to the server is bound to keep it confidential;
- keep it secure with the measures described in the privacy policy, including encrypted connections, isolated
  containers and memory-only storage;
- use only the service providers listed in the privacy policy ({{hosting_provider}}{{#cdn_provider}}, {{cdn_provider}}
  for delivering the site{{/cdn_provider}}{{#ai_enabled}}, and Anthropic when you start an AI tool{{/ai_enabled}}),
  bound by data processing terms, and announce any new one on the privacy
  page at least 30 days before using it — if you object, stop using the service;
- help you, as far as we reasonably can, to answer requests from people about their data and to meet your own
  security, breach-notification and impact-assessment duties;
- delete the data automatically as described above, so it's gone within hours at most;
- give you the information you need to check all this. The service is open source, so the code doing the work is
  public; ask us anything else at {{contact_email}}.

You're responsible for having a lawful reason to process that data with us, and for not using {{#ai_enabled}}the AI
tools or {{/ai_enabled}}any tool for data your organisation's rules don't allow to leave its systems.
<!-- VERIFY: these bullets track GDPR Art. 28(3)(a)–(h). Business users who want a signed DPA can be offered this
     section as one. -->

**Other people in your files.** Only upload files you have the right to use. If they show or describe other people,
make sure you're allowed to process that information.

**Signatures and certificates.** When you sign a PDF, you're responsible for the signature being yours to make. If
you use your own certificate file, we use it only for that signing job and delete it straight after; we don't check
who you are, so a signature made here proves only what your certificate proves.

## 4. What you must not do

Don't use the service to do anything illegal or harmful. In particular, don't upload, create or process:

- **child sexual abuse material** of any kind — we will report it to the authorities wherever the law requires;
- intimate images of anyone without their consent, or content that sexually exploits anyone;
- **forged or falsified documents** — fake IDs, certificates, signatures, stamps, receipts or official records — or
  anything meant to pass off a document as something it isn't;
- deceptive documents about real people or events meant to mislead others, or content impersonating someone else;
- material that infringes someone else's copyright, trademark or other rights — including unlocking PDFs, or
  removing watermarks or restrictions, that you aren't entitled to remove or unlock;
- content that harasses, threatens or defames people, promotes violence or hatred against groups, or that's unlawful
  where you are or in {{operator_country}};
- malware, or files designed to attack or crash our software.

And don't misuse the service itself:

- don't try to break, overload, probe or get around its limits and security (no denial-of-service, no vulnerability
  scanning without permission — see section 7 for how to report problems);
- don't use automated tools to send large volumes of requests, or resell the service as an API — if you need that,
  run your own copy (section 9);
{{#url_tool_enabled}}- don't use the HTML to PDF tool to reach private networks, attack websites, or get around paywalls
  and logins;
{{/url_tool_enabled}}{{#ai_enabled}}- don't use the AI tools for anything Anthropic's [Usage Policy](https://www.anthropic.com/legal/aup)
  forbids;
{{/ai_enabled}}- don't use the service to break anyone else's rules that bind you, such as an exam's or employer's.

Because we don't look at files, we usually won't know what you upload. If we learn of a breach of this section, we
may block access, keep the minimum information needed to deal with it, and report it where the law requires.
<!-- VERIFY: this list covers the user-notice items of India's IT (Intermediary Guidelines…) Rules 2021 r.3(1)(b) (as
     amended 2022/2023/Feb 2026: synthetic "forged documents", deceptive depictions) in plain words — have it checked
     if you rely on intermediary safe harbour (IT Act s.79). -->

## 5. Results{{#ai_enabled}}, and the AI tools{{/ai_enabled}}

We work hard to make results correct, but **check them before you rely on them** — especially compressed files,
converted documents, OCR text, redacted documents (check that everything you wanted removed is gone) and anything
you'll sign or submit.

{{#ai_enabled}}The AI tools ({{ai_tools}}) use Claude, a model made by Anthropic. **AI output can be wrong, incomplete,
out of date or made up.** Facts in a summary or translation should not be relied on without checking them against the
original. AI output isn't legal, medical, financial or other professional advice. Results are labelled as
AI-generated; please don't remove that label when you pass them on as if a person wrote them.
<!-- VERIFY: Anthropic Commercial Terms §D.3 requires customers to tell users that factual assertions in Outputs
     should not be relied upon without independently checking their accuracy. Keep the sentence above. -->
{{/ai_enabled}}

## 6. Copyright and other complaints

Files on the service are private to the person who uploaded them, aren't published anywhere, and are deleted within
hours at most, so there's normally nothing for us to take down. If you believe the service is being used to infringe
your rights, or you have any other complaint about content or conduct, write to **{{abuse_email}}** with:

- your name and contact details;
- what you believe is being infringed or what's wrong, and where you saw it (a URL or other detail that lets us find
  it);
- a statement that you believe in good faith the use isn't authorised by the owner, its agent or the law, and that
  your notice is accurate — and, for copyright, that you're the owner or authorised to act for them;
- your physical or electronic signature.

{{#dmca_agent}}Our designated agent under the US Digital Millennium Copyright Act is {{dmca_agent}}, as registered
with the US Copyright Office.

{{/dmca_agent}}**Grievance officer (India).** Under India's Information Technology rules, our grievance officer is
**{{grievance_officer_name}}** ({{contact_email}}). We acknowledge complaints within 24 hours and resolve them within
{{grievance_days}} days. Orders from courts or authorities are handled within the time the law sets.

**Contact for authorities (EU Digital Services Act).** Authorities, the European Commission and the European Board for
Digital Services can reach us at {{contact_email}}, in English.

## 7. Security problems

If you find a vulnerability, please tell us at {{security_email}} (or privately through our
[security policy]({{source_url}}/security/policy)) and give us a reasonable time to fix it before going public.
Don't access other people's data, degrade the service or run automated scanners against it — test against your own
copy instead. We won't take legal action over good-faith research that follows these rules.

## 8. No warranty, and limits on our liability

The service is free and provided **"as is" and "as available"**, without warranties of any kind — including that it
will be accurate, uninterrupted, secure, or fit for a particular purpose — to the fullest extent the law allows.

To the fullest extent the law allows, we're not liable for any indirect or consequential loss, or for lost data,
profits, opportunities or time, arising from your use of the service; and our total liability for everything related
to the service is limited to **{{liability_cap}}**.

Nothing in these terms limits liability that can't be limited by law — such as for death or personal injury caused by
negligence, or for fraud — or takes away rights you have as a consumer under the law of the country where you live.
<!-- VERIFY: liability cap amount and wording with a lawyer for the governing law; EU/UK consumer law limits how far
     a free service can exclude liability. -->

## 9. Open source

The software behind the service is free software, licensed under the
[GNU Affero General Public License, version 3](https://www.gnu.org/licenses/agpl-3.0.html). You can get the exact source
code of the version running here — {{version}}{{#source_revision}}, commit `{{source_revision}}`{{/source_revision}} —
at no charge from [{{source_tree_url}}]({{source_tree_url}}), and you may run, study, modify and share it under that
licence. The licence, not these terms, governs your use of the code; these terms govern your use of our hosted
service. Third-party components — fonts and programs — keep their own licences, listed on our
[credits page](/credits).

## 10. Changes to these terms

We may update these terms. We'll change the date at the top, and for significant changes we'll flag them on the site
for at least 30 days before they apply. If you keep using the service after that, the new terms apply. Earlier
versions are in the project's history on GitHub.

## 11. Law and disputes

These terms are governed by the laws of {{jurisdiction}}, and the courts of {{jurisdiction}} have jurisdiction — but
if you're a consumer, you keep the protection of the mandatory laws of the country where you live and can bring a
claim there. Please talk to us first at {{contact_email}}: most problems can be sorted out by email.

If any part of these terms turns out to be unenforceable, the rest still applies. Not enforcing a term straight away
doesn't mean we've given up the right to.
