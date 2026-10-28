# FCA Register contact collector

Uses the **official** FCA Financial Services Register API (no HTML scraping).

## Setup (5 minutes, all free)
1. Register at https://register.fca.org.uk/Developer/s/ and copy your API key.
2. Create a **private** GitHub repo and push these files.
3. Repo -> Settings -> Secrets and variables -> Actions -> add `FCA_EMAIL` and `FCA_KEY`.
4. Actions tab -> "Collect FCA contacts" -> Run workflow.

## Step 1: explore
Run with mode `explore` and a term like `Barclays`. Download the `fca-output` artifact and read
`explore.txt` to see exactly which fields the Address endpoint returns on your account
(the script auto-detects any field whose name contains "email").

## Step 2: collect
Run mode `collect` with terms like `insurance,mortgage,wealth`. Download `contacts.csv`.

## Search options known so far
- `/Search?q=<text>&type=firm|individual|fund`
- `/CommonSearch?q=<text>`
- `/Firm/{FRN}`, `/Address`, `/Names`, `/Individuals`, `/AR` (appointed reps), `/Permissions`, `/Requirements`

There is no "list all" endpoint, so coverage comes from many varied search terms
(trade types, towns, common name words, A-Z prefixes).

## Responsible use
Emails are firm-published business contacts. The register does not publish personal emails for
individuals. Sole-trader addresses may be personal data under UK GDPR, and marketing is covered
by PECR. Keep the repo private and don't commit your API key or the results.
