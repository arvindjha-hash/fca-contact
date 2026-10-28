import json, re, time
import urllib.parse, urllib.robotparser
import pandas as pd
import requests
import streamlit as st

BASE = "https://register.fca.org.uk/services/V0.1"
st.set_page_config(page_title="FCA Register Lookup", layout="wide")


def secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


# ---------- optional password gate ----------
_pw = secret("APP_PASSWORD")
if _pw and not st.session_state.get("authed"):
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == _pw:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

with st.sidebar:
    st.header("Settings")
    fca_email = st.text_input("FCA API email", value=secret("FCA_EMAIL"))
    fca_key = st.text_input("FCA API key", value=secret("FCA_KEY"), type="password")
    delay = st.slider("Delay between API calls (sec)", 0.5, 3.0, 1.1, 0.1)
    max_items = st.number_input("Max results per search term", 1, 2000, 100)
    st.caption("Each result costs 3-6 API calls (about 1 sec each), so large searches take time.")


# ---------- API helpers ----------
@st.cache_data(ttl=3600, show_spinner=False)
def api_get(url, params_json, em, key, wait):
    """Raises RuntimeError on failure (failures are not cached)."""
    time.sleep(wait)
    params = json.loads(params_json) if params_json else None
    headers = {"X-AUTH-EMAIL": em, "X-AUTH-KEY": key, "Content-Type": "application/json"}
    last = ""
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            last = f"network error: {e}"
            time.sleep(2)
            continue
        if r.status_code == 429:
            last = f"HTTP 429 rate limited | {r.url}"
            time.sleep(8 * (attempt + 1))
            continue
        if r.status_code == 404:
            return None
        if r.status_code >= 500 and "Too many query rows" in r.text:
            raise RuntimeError(f"Term too broad (FCA limit: over 50,000 matches) | {r.url}")
        if r.status_code >= 500:
            last = f"HTTP {r.status_code} server error | {r.url} | {r.text[:200]}"
            if attempt >= 1:
                break
            time.sleep(3)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} | {r.url} | {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return None
    raise RuntimeError(f"Failed after retries: {last}")


def log_error(msg):
    st.session_state.setdefault("errlog", []).append(
        {"time": time.strftime("%H:%M:%S"), "error": msg})


def call(url, params=None):
    try:
        return api_get(url, json.dumps(params) if params else "", fca_email, fca_key, delay)
    except Exception as e:
        log_error(str(e))
        return None


def walk(obj, key=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, key)
    else:
        yield key, obj


def values_for(obj, needle):
    return sorted({str(v).strip() for k, v in walk(obj)
                   if needle in k.lower() and str(v).strip() and not str(v).startswith("http")})


def clean_name(n):
    return re.sub(r"\s*\(Postcode:.*\)\s*$", "", n or "").strip()


def pick(item, *needles):
    for n in needles:
        for k, v in item.items():
            if n in k.lower() and not str(v).startswith("http") and str(v).strip():
                return str(v).strip()
    return ""


def too_broad_since(n):
    return any("too broad" in e["error"] for e in st.session_state.get("errlog", [])[n:])


def paged(url, params, limit, max_pages=60):
    n_before = len(st.session_state.get("errlog", []))
    items, seen = [], set()
    for page in range(1, max_pages + 1):
        data = call(url, {**params, "pgnb": page})
        if data is None and page == 1 and not too_broad_since(n_before):
            data = call(url, params or None)  # retry without paging parameter
        batch = (data or {}).get("Data") or []
        new = []
        for i in batch:
            sig = json.dumps(i, sort_keys=True, default=str)
            if sig not in seen:
                seen.add(sig)
                new.append(i)
        if not new:
            break
        items.extend(new)
        if len(items) >= limit:
            break
    return items[: int(limit)]


NARROW_DEFAULT = ("Planning, Services, Management, Advisers, Advisory, Solutions, Group, Partners, "
                  "Associates, Consultants, Wealth, Mortgage, Independent, Insurance, Investment, "
                  "Pensions, Capital, Limited, LLP")


def search(term, rtype, narrow_words=None):
    n0 = len(st.session_state.get("errlog", []))
    items = paged(f"{BASE}/Search", {"q": term, "type": rtype}, max_items)
    if not items and narrow_words and too_broad_since(n0):
        seen, out = set(), []
        for w in narrow_words:
            if len(out) >= max_items:
                break
            for it in paged(f"{BASE}/Search", {"q": f"{term} {w}", "type": rtype}, max_items - len(out)):
                if it.get("URL") not in seen:
                    seen.add(it.get("URL"))
                    out.append(it)
        items = out
    return items


def norm_url(raw):
    first = (raw or "").split(";")[0].strip()
    if not first:
        return ""
    return first if first.lower().startswith("http") else "https://" + first


def firm_contact(frn):
    addr = call(f"{BASE}/Firm/{frn}/Address") or {}
    return {
        "Website": norm_url("; ".join(values_for(addr, "website"))),
        "Email": "; ".join(values_for(addr, "email")),
        "Phone": "; ".join(values_for(addr, "phone")),
        "Town": "; ".join(values_for(addr, "town")),
        "Postcode": "; ".join(values_for(addr, "postcode")),
    }


def firm_name(frn):
    d = call(f"{BASE}/Firm/{frn}") or {}
    names = values_for(d, "organisation name")
    return names[0] if names else ""


def firm_ids(obj):
    """Find firm reference numbers anywhere in a response (URLs or FRN-like keys)."""
    ids = []
    for k, v in walk(obj):
        sv = str(v)
        m = re.search(r"/Firm/(\d{3,})", sv)
        if m:
            ids.append(m.group(1))
        elif ("frn" in k.lower() or "firm reference" in k.lower()) and sv.strip().isdigit():
            ids.append(sv.strip())
    return list(dict.fromkeys(ids))


# ---------- optional: published contact email from a firm's own website ----------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
FILE_EXT = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "woff", "woff2"}
SOCIAL = ("facebook.", "linkedin.", "twitter.", "instagram.", "x.com", "youtube.")
UA = {"User-Agent": "Mozilla/5.0 (compatible; FCA-contact-lookup; polite single-page fetch)"}


@st.cache_data(ttl=86400, show_spinner=False)
def site_emails(site_url):
    """Look for a published email on the firm's own domain (home + contact pages). Respects robots.txt."""
    if not site_url:
        return ""
    p = urllib.parse.urlparse(site_url)
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if not host or any(sn in host for sn in SOCIAL):
        return ""
    root = f"{p.scheme}://{p.netloc}"
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = requests.get(root + "/robots.txt", timeout=8, headers=UA)
        rp.parse(r.text.splitlines() if r.ok else [])
    except Exception:
        rp.parse([])
    found = []
    for path in ["", "/contact", "/contact-us", "/contact.html", "/about-us"]:
        url = root + path
        try:
            if not rp.can_fetch("*", url):
                continue
            r = requests.get(url, timeout=8, headers=UA)
        except Exception:
            continue
        if not r.ok or "html" not in r.headers.get("content-type", ""):
            continue
        for m in EMAIL_RE.findall(r.text[:600000]):
            e = m.lower().strip(".")
            dom = e.split("@")[1]
            if e.rsplit(".", 1)[-1] in FILE_EXT:
                continue
            if (dom == host or dom.endswith("." + host) or host.endswith("." + dom)) and e not in found:
                found.append(e)
        if found:
            break
        time.sleep(0.5)
    return "; ".join(found[:3])


# ---------- row builders ----------
EMPTY_FIRM = {"Company": "", "FRN": "", "Website": "", "Email": "", "Phone": "", "Town": "", "Postcode": ""}


def rows_individual(item):
    url = item.get("URL", "")
    details = call(url) or {}
    blobs = [details]
    follow = []
    for k, v in walk(details):
        if isinstance(v, str) and v.startswith("http") and "register.fca.org.uk" in v:
            low = v.lower()
            if "disciplinary" in low or v.split("?")[0] == url.split("?")[0]:
                continue
            if "/individual" in low or "role" in k.lower():
                follow.append(v)
    for u in list(dict.fromkeys(follow))[:4]:
        blobs.append(call(u) or {})
    ids = []
    for b in blobs:
        ids += firm_ids(b)
    ids = list(dict.fromkeys(ids))
    base = {"Name": item.get("Name", ""), "IRN": item.get("Reference Number", ""),
            "Status": item.get("Status", "")}
    if not ids:
        return [{**base, **EMPTY_FIRM}]
    return [{**base, "Company": firm_name(f), "FRN": f, **firm_contact(f)} for f in ids]


def rows_firm(item):
    frn = str(item.get("Reference Number", ""))
    return [{"Company": clean_name(item.get("Name", "")), "FRN": frn,
             "Status": item.get("Status", ""), **firm_contact(frn)}]


def rows_firm_people(item, per_firm):
    frn = str(item.get("Reference Number", ""))
    company = clean_name(item.get("Name", ""))
    contact = firm_contact(frn)
    people = paged(f"{BASE}/Firm/{frn}/Individuals", {}, per_firm, max_pages=20)
    if not people:
        return [{"Name": "", "IRN": "", "Status": item.get("Status", ""),
                 "Company": company, "FRN": frn, **contact}]
    return [{"Name": pick(p, "name"), "IRN": pick(p, "irn", "reference"),
             "Status": pick(p, "status"), "Company": company, "FRN": frn, **contact}
            for p in people]


# ---------- UI ----------
st.title("FCA Register lookup")
tab_search, tab_debug = st.tabs(["Search", "Raw API viewer"])

with tab_search:
    mode = st.radio("Search for",
                    ["People at firms (largest pool)", "Individuals by name", "Firms"],
                    horizontal=True)
    if mode.startswith("People"):
        st.caption("Type firm keywords (e.g. `insurance broker`, `mortgage`, `wealth`, a town). "
                   "For each firm found, the app lists its approved people with company, website, "
                   "email and phone filled in.")
        per_firm = st.number_input("Max people per firm", 1, 500, 25)
    elif mode.startswith("Individuals"):
        st.caption("One term per line. A surname alone may return nothing, so you can combine with first names below.")
        per_firm = 0
    else:
        per_firm = 0

    terms_text = st.text_area("One search term per line", height=110)
    firsts_text = ""
    if mode.startswith("Individuals"):
        firsts_text = st.text_input("Optional: first names to combine with every line (comma-separated)",
                                    placeholder="John, David, Sarah, Michael")

    auto_narrow = st.checkbox("Auto-narrow terms that are too broad (adds extra words)", value=True,
                              help="If the FCA says a term matches over 50,000 records, the app retries "
                                   "it combined with each word below.")
    narrow_text = st.text_input("Words to add when a term is too broad", NARROW_DEFAULT) if auto_narrow else ""
    narrow_words = [w.strip() for w in narrow_text.split(",") if w.strip()]
    find_site_email = st.checkbox(
        "Also look for a contact email on each firm's own website (slower)",
        help="Visits the firm's home/contact page and collects emails on the firm's own domain. "
             "These are the firm's published business contacts, not personal emails.")
    ready = bool(fca_email and fca_key)
    if not ready:
        st.info("Add your FCA API email and key in the sidebar.")
    go = st.button("Search", type="primary", disabled=not ready)

    if go and terms_text.strip():
        terms = [t.strip() for t in terms_text.splitlines() if t.strip()]
        if firsts_text.strip():
            firsts = [f.strip() for f in firsts_text.split(",") if f.strip()]
            terms = [f"{f} {t}" for t in terms for f in firsts]
        rows, summary = [], []
        st.session_state["errlog"] = []
        bar = st.progress(0.0, text="Searching...")
        for ti, term in enumerate(terms):
            rtype = "individual" if mode.startswith("Individuals") else "firm"
            found = search(term, rtype, narrow_words)
            summary.append({"search term": term, "results found": len(found)})
            for pi, item in enumerate(found):
                bar.progress((ti + (pi + 1) / max(len(found), 1)) / len(terms),
                             text=f"{term}: {item.get('Name', '')[:60]}")
                try:
                    if mode.startswith("People"):
                        rows.extend(rows_firm_people(item, per_firm))
                    elif mode.startswith("Individuals"):
                        rows.extend(rows_individual(item))
                    else:
                        rows.extend(rows_firm(item))
                except Exception as e:
                    log_error(f"while processing {item.get('Name', '')[:50]}: {e}")
        st.session_state["summary"] = summary
        bar.empty()
        df_new = pd.DataFrame(rows)
        if find_site_email and not df_new.empty and "Website" in df_new.columns:
            sites = [w for w in df_new["Website"].unique() if w]
            sbar = st.progress(0.0, text="Checking firm websites...")
            mapping = {}
            for i, w in enumerate(sites):
                mapping[w] = site_emails(w)
                sbar.progress((i + 1) / len(sites), text=f"Checking {w[:60]}")
            sbar.empty()
            df_new["Email"] = df_new.apply(lambda r: r["Email"] or mapping.get(r["Website"], ""), axis=1)
        st.session_state["df"] = df_new

    df = st.session_state.get("df")
    if df is not None and not df.empty:
        c1, c2, c3, c4, c5 = st.columns(5)
        only_company = c1.checkbox("Only with company")
        only_web = c1.checkbox("Only with website")
        only_email = c2.checkbox("Only with email")
        only_phone = c3.checkbox("Only with phone")
        status_opts = sorted(x for x in df["Status"].dropna().unique() if x)
        default_status = [s for s in status_opts if s.lower() in ("active", "registered", "authorised")] or status_opts
        status_sel = c4.multiselect("Status", status_opts, default=default_status)
        company_q = c5.text_input("Company contains")

        f = df.copy()
        if only_company:
            f = f[f["Company"].astype(bool)]
        if only_web:
            f = f[f["Website"].astype(bool)]
        if only_email:
            f = f[f["Email"].astype(bool)]
        if only_phone:
            f = f[f["Phone"].astype(bool)]
        if status_sel:
            f = f[f["Status"].isin(status_sel)]
        if company_q:
            f = f[f["Company"].str.contains(company_q, case=False, na=False)]
        f = f.drop_duplicates()
        f["Notes"] = ""

        st.caption(f"{len(f)} of {len(df)} rows. Click any cell to edit; add or delete rows at will.")
        edited = st.data_editor(
            f, num_rows="dynamic", use_container_width=True, hide_index=True,
            column_config={"Website": st.column_config.LinkColumn("Website", display_text=r"https?://(.*)")},
        )
        st.download_button("Download CSV", edited.to_csv(index=False).encode("utf-8"),
                           "fca_results.csv", "text/csv")
    elif go:
        st.warning("No results found.")

    summ = st.session_state.get("summary")
    errs = st.session_state.get("errlog", [])
    if summ:
        with st.expander("Search summary (results per term)", expanded=bool(errs)):
            st.dataframe(pd.DataFrame(summ), use_container_width=True, hide_index=True)
    if errs:
        st.error(f"{len(errs)} API error(s) occurred. Details below. The search continued with other terms.")
        with st.expander("Error log", expanded=True):
            elog = pd.DataFrame(errs)
            st.dataframe(elog, use_container_width=True, hide_index=True)
            st.download_button("Download error log", elog.to_csv(index=False).encode("utf-8"),
                               "error_log.csv", "text/csv")
            st.caption("HTTP 500 usually means the FCA server rejected that search (often a term that "
                       "matches too many firms). Try a more specific term like `Financial Planning`. "
                       "401/403 means a wrong email or key.")

with tab_debug:
    st.write("See exactly what the API returns, to check which fields exist.")
    ready = bool(fca_email and fca_key)
    irn = st.text_input("Individual: paste the URL from a search result, or search a name")
    q = st.text_input("Or search term", "")
    kind = st.selectbox("Type", ["individual", "firm"])
    if st.button("Show raw result") and ready and (q or irn):
        res = search(q, kind)[:2] if q else [{"URL": irn}]
        for r in res:
            st.write("Search item"); st.json(r)
            d = call(r.get("URL", "")) or {}
            st.write("Detail record"); st.json(d)
            for k, v in walk(d):
                if isinstance(v, str) and v.startswith("http") and "disciplinary" not in v.lower():
                    st.write(f"Linked: {k}")
                    st.json(call(v) or {})
                    break
    st.divider()
    frn_in = st.text_input("Check a firm by FRN")
    if st.button("Show raw firm Address + Individuals") and frn_in.strip() and ready:
        st.json(call(f"{BASE}/Firm/{frn_in.strip()}/Address") or {})
        st.json(call(f"{BASE}/Firm/{frn_in.strip()}/Individuals") or {})
