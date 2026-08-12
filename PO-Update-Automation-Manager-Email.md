Subject: Proposal: automating PO shipping updates for Paula (NetSuite)

Hi [Manager's name],

I'd like your approval to build a small automation that saves Paula (Supply Chain) a meaningful chunk of manual work every week, and I wanted to walk you through what it does, what it costs, and how we're keeping the risk low before asking for the go-ahead.

**The problem today**

When a vendor ships an order, they email Paula a packing slip and shipping advice with the real final quantities and dates. Right now she reads those documents by hand and manually re-types the numbers into NetSuite, line by line, for every style/color/size on the PO — updating Quantity, Expected Receipt Date, and two other date fields each time. She does this for 10–20 shipments a week. It's slow, repetitive, and exactly the kind of task where a mistyped number or a missed line quietly causes a downstream inventory or receiving problem.

**What this automation does**

It reads the vendor's email attachments, figures out which PO and item lines they correspond to in NetSuite, and works out exactly what changed versus what's currently on file — quantity and all three date fields. It then sends Paula a short summary showing the old value next to the new one, and she approves or rejects it with one click. Only after she approves does anything actually get written to NetSuite. Nothing changes in NetSuite without her sign-off, ever — that's a permanent design decision, not a "training wheels" step we plan to remove later.

**Why it's worth doing**

Paula gets hours back every week that are currently spent on manual data entry, and the pipeline is built to flag anything it isn't confident about rather than guess — so it reduces the error risk of manual re-entry instead of just relocating it. It's a good return for a small build: this doesn't require new headcount or new vendor tooling, just a small amount of hosting.

**How we're keeping risk low**

- Everything is built and tested against NetSuite's sandbox first — production is only touched after the whole flow is proven end-to-end.
- The human approval step above is permanent, not temporary.
- We've already validated the hard technical parts: NetSuite read/write access under a properly scoped, least-privilege service role (not an admin account), and document parsing against a real vendor file with a 100% match against the vendor's own numbers.

**Hosting and cost**

This runs on infrastructure we already use — Microsoft 365/Outlook for reading the vendor emails, and Azure (serverless, pay-per-use, no server to maintain) for hosting. Rough estimates:

- Build time: ~3–4 weeks, part-time, done by me — no contractor cost.
- Hosting: ~$5–20/month (Azure Functions + a small database, both serverless/pay-per-use).
- AI usage (Anthropic API, for reading each vendor's differently-formatted documents): likely single-digit dollars per month at this volume. Note this is a separate, usage-based API cost from my Claude subscription — small, but real, and it scales with volume if that grows later.
- NetSuite and Microsoft 365: $0 incremental — both already covered under existing accounts.

I've attached a one-page diagram of the flow. Happy to walk through it live or answer questions — what I need from you is a green light to move forward with the build.

Thanks,
Kiko
