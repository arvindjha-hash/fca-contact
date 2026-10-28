#!/usr/bin/env python3
"""
FCA Register contact collector (official API only).

Usage:
  python fca_contacts.py --explore "Barclays"          # print raw API responses to discover fields
  python fca_contacts.py --queries "insurance,mortgage broker" --out contacts.csv
  python fca_contacts.py --queries-file terms.txt --max-firms 500

Env vars: FCA_EMAIL, FCA_KEY  (from https://register.fca.org.uk/Developer/s/)

Notes:
- Only firm-published business contact details are collected.
- The register does not list personal emails for individuals.
- Check UK GDPR / PECR before using results for marketing.
"""
import argparse, csv, json, os, re, sys, time
import requests

BASE = "https://register.fca.org.uk/services/V0.1"
DELAY = 1.1  # stay under the FCA rate limit

S = requests.Session()
S.headers.update({
    "X-AUTH-EMAIL": os.environ.get("FCA_EMAIL", ""),
    "X-AUTH-KEY": os.environ.get("FCA_KEY", ""),
    "Content-Type": "application/json",
})


def get(url, params=None, retries=4):
    for attempt in range(retries):
        time.sleep(DELAY)
        r = S.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return None
    return None


def search_firms(term, rtype="firm", max_pages=200):
    """Yield search result items, following pagination."""
    url, params, pages = f"{BASE}/Search", {"q": term, "type": rtype}, 0
    while url and pages < max_pages:
        data = get(url, params)
        params = None  # 'Next' URLs already carry the query
        if not data:
            return
        for item in data.get("Data") or []:
            yield item
        nxt = (data.get("Pagination") or {}).get("Next")
        url = nxt if nxt else None
        pages += 1


def frn_of(item):
    ref = item.get("Reference Number") or item.get("FRN")
    if ref:
        return str(ref)
    m = re.search(r"/Firm/(\d+)", item.get("URL", ""))
    return m.group(1) if m else None


def find_keys(obj, needle):
    """Recursively collect values whose key contains `needle`."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if needle in k.lower() and isinstance(v, (str, int)) and str(v).strip():
                out.append(str(v).strip())
            out.extend(find_keys(v, needle))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(find_keys(v, needle))
    return out


def firm_contacts(frn):
    addr = get(f"{BASE}/Firm/{frn}/Address") or {}
    emails = sorted(set(find_keys(addr, "email")))
    phones = sorted(set(find_keys(addr, "phone")))
    sites = sorted(set(find_keys(addr, "website")))
    return emails, phones, sites


def explore(term):
    print("== SEARCH (firm) ==")
    first = None
    for item in search_firms(term, max_pages=1):
        first = first or item
        print(json.dumps(item, indent=2)[:600])
        break
    if first and frn_of(first):
        frn = frn_of(first)
        for path in ("", "/Address", "/Names", "/Individuals", "/AR"):
            print(f"\n== Firm/{frn}{path} ==")
            print(json.dumps(get(f"{BASE}/Firm/{frn}{path}"), indent=2)[:2500])
    print("\n== CommonSearch ==")
    print(json.dumps(get(f"{BASE}/CommonSearch", {"q": term}), indent=2)[:1500])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--explore", metavar="TERM")
    ap.add_argument("--queries", help="comma-separated search terms")
    ap.add_argument("--queries-file", help="one search term per line")
    ap.add_argument("--max-firms", type=int, default=300)
    ap.add_argument("--include-inactive", action="store_true")
    ap.add_argument("--only-with-email", action="store_true")
    ap.add_argument("--out", default="contacts.csv")
    a = ap.parse_args()

    if not (os.environ.get("FCA_EMAIL") and os.environ.get("FCA_KEY")):
        sys.exit("Set FCA_EMAIL and FCA_KEY environment variables.")

    if a.explore:
        return explore(a.explore)

    terms = []
    if a.queries:
        terms += [t.strip() for t in a.queries.split(",") if t.strip()]
    if a.queries_file:
        terms += [l.strip() for l in open(a.queries_file) if l.strip()]
    if not terms:
        sys.exit("Provide --queries or --queries-file (or use --explore).")

    seen, rows = set(), []
    for term in terms:
        for item in search_firms(term):
            if len(seen) >= a.max_firms:
                break
            frn = frn_of(item)
            if not frn or frn in seen:
                continue
            status = str(item.get("Status", ""))
            if not a.include_inactive and "authorised" not in status.lower() and status:
                continue
            seen.add(frn)
            emails, phones, sites = firm_contacts(frn)
            if a.only_with_email and not emails:
                continue
            rows.append({
                "frn": frn, "name": item.get("Name", ""), "status": status,
                "search_term": term, "emails": "; ".join(emails),
                "phones": "; ".join(phones), "websites": "; ".join(sites),
                "register_url": f"https://register.fca.org.uk/s/firm?id={frn}",
            })
            print(f"[{len(seen)}] {frn} {item.get('Name','')} -> {len(emails)} email(s)", flush=True)

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["frn","name","status","search_term","emails","phones","websites","register_url"])
        w.writeheader(); w.writerows(rows)
    print(f"Wrote {len(rows)} firms to {a.out}")


if __name__ == "__main__":
    main()
