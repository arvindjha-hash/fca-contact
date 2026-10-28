import json, time
import pandas as pd
import requests
import streamlit as st

BASE = "https://register.fca.org.uk/services/V0.1"
st.set_page_config(page_title="FCA Register Lookup", layout="wide")


# ---------- credentials ----------
def secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


with st.sidebar:
    st.header("Settings")
    fca_email = st.text_input("FCA API email", value=secret("FCA_EMAIL"))
    fca_key = st.text_input("FCA API key", value=secret("FCA_KEY"), type="password")
    delay = st.slider("Delay between API calls (sec)", 0.5, 3.0, 1.1, 0.1)
    max_people = st.number_input("Max individuals per search", 1, 200, 20)
    st.caption("Tip: on Streamlit Cloud, store these in Secrets instead of typing them.")


# ---------- API helpers ----------
@st.cache_data(ttl=3600, show_spinner=False)
def api_get(url, params_json, em, key, wait):
    time.sleep(wait)
    params = json.loads(params_json) if params_json else None
    headers = {"X-AUTH-EMAIL": em, "X-AUTH-KEY": key, "Content-Type": "application/json"}
    for attempt in range(4):
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 429:
            time.sleep(8 * (attempt + 1))
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return None
    return None


def call(url, params=None):
    return api_get(url, json.dumps(params) if params else "", fca_email, fca_key, delay)


def walk(obj, key=""):
    """Yield (key, value) for every leaf in nested JSON."""
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


def search(name, rtype="individual"):
    items, seen = [], set()
    for page in range(1, 30):
        data = call(f"{BASE}/Search", {"q": name, "type": rtype, "pgnb": page})
        batch = [i for i in ((data or {}).get("Data") or []) if i.get("URL") not in seen]
        if not batch:
            break
        for i in batch:
            seen.add(i.get("URL"))
            items.append(i)
        if len(items) >= max_people:
            break
    return items[: int(max_people)]


def find_firms(obj):
    """Find (frn, name) pairs anywhere in a roles/details response."""
    firms = {}
    def rec(node):
        if isinstance(node, dict):
            frn = next((str(v) for k, v in node.items()
                        if ("frn" in k.lower() or "firm reference" in k.lower()) and str(v).strip()), None)
            if frn:
                name = next((str(v) for k, v in node.items()
                             if "name" in k.lower() and not str(v).startswith("http") and str(v).strip()), "")
                firms[frn] = name
            for v in node.values():
                rec(v)
        elif isinstance(node, list):
            for v in node:
                rec(v)
    rec(obj)
    return firms


def norm_url(raw):
    first = (raw or "").split(";")[0].strip()
    if not first:
        return ""
    return first if first.lower().startswith("http") else "https://" + first


def firm_contact(frn):
    addr = call(f"{BASE}/Firm/{frn}/Address") or {}
    return {
        "Website": norm_url("; ".join(values_for(addr, "website"))),
        "Phone": "; ".join(values_for(addr, "phone")),
        "Town": "; ".join(values_for(addr, "town")),
        "Postcode": "; ".join(values_for(addr, "postcode")),
    }


def lookup_firm(item):
    frn = str(item.get("Reference Number", ""))
    return [{"Company": item.get("Name", ""), "FRN": frn,
             "Status": item.get("Status", ""), **firm_contact(frn)}]


def lookup_person(item):
    url = item.get("URL", "")
    details = call(url) or {}
    # follow any role-related link the individual record exposes
    role_urls = [v for k, v in walk(details) if isinstance(v, str) and v.startswith("http")
                 and "role" in (k + v).lower()]
    roles = [call(u) or {} for u in role_urls[:2]]
    firms = {}
    for blob in [details] + roles:
        firms.update(find_firms(blob))
    base = {
        "Name": item.get("Name", ""),
        "IRN": item.get("Reference Number", ""),
        "Status": item.get("Status", ""),
    }
    rows = []
    if not firms:
        rows.append({**base, "Company": "", "FRN": "", "Website": "", "Phone": "",
                     "Town": "", "Postcode": ""})
    for frn, fname in firms.items():
        rows.append({**base, "Company": fname, "FRN": frn, **firm_contact(frn)})
    return rows


# ---------- UI ----------
st.title("FCA Register lookup")
tab_search, tab_debug = st.tabs(["Search", "Raw API viewer"])

with tab_search:
    mode = st.radio("Search for", ["Individuals", "Firms"], horizontal=True)
    names = st.text_area("One search term per line",
                         placeholder="John Smith\nJane Doe" if mode == "Individuals"
                         else "insurance broker\nmortgage\nWealth management", height=110)
    ready = bool(fca_email and fca_key)
    if not ready:
        st.info("Add your FCA API email and key in the sidebar.")
    go = st.button("Search", type="primary", disabled=not ready)

    if go and names.strip():
        rows, terms = [], [n.strip() for n in names.splitlines() if n.strip()]
        bar = st.progress(0.0, text="Searching...")
        try:
            for ti, term in enumerate(terms):
                found = search(term, "individual" if mode == "Individuals" else "firm")
                for pi, item in enumerate(found):
                    bar.progress((ti + (pi + 1) / max(len(found), 1)) / len(terms),
                                 text=f"{term}: {item.get('Name', '')}")
                    rows.extend(lookup_person(item) if mode == "Individuals" else lookup_firm(item))
        except requests.HTTPError as e:
            st.error(f"API error: {e}. Check your email/key.")
        bar.empty()
        st.session_state["df"] = pd.DataFrame(rows)

    df = st.session_state.get("df")
    if df is not None and not df.empty:
        c1, c2, c3, c4 = st.columns(4)
        only_web = c1.checkbox("Only with website")
        only_phone = c2.checkbox("Only with phone")
        status_opts = sorted(x for x in df["Status"].dropna().unique() if x)
        status_sel = c3.multiselect("Status", status_opts, default=status_opts)
        company_q = c4.text_input("Company contains")

        f = df.copy()
        if only_web:
            f = f[f["Website"].astype(bool)]
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

with tab_debug:
    st.write("See exactly what the API returns, to check which fields exist.")
    q = st.text_input("Search term", "")
    kind = st.selectbox("Type", ["individual", "firm"])
    if st.button("Show raw result") and q and fca_email and fca_key:
        for r in search(q, kind)[:3]:
            st.json(r)
            st.json(call(r.get("URL", "")) or {})
