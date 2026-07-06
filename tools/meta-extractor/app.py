"""
Meta Details Extractor
Extract H1, Meta Title, Meta Description & Meta Keywords from multiple URLs.
Built with Python + Streamlit | by Mahalakshmi Marimuthu
"""

import concurrent.futures
import io

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- page config
st.set_page_config(
    page_title="Meta Details Extractor",
    page_icon="🔍",
    layout="wide",
)

# ---------------------------------------------------------------- custom CSS
st.markdown(
    """
    <style>
    /* App background */
    .stApp { background-color: #0a0a0f; }

    /* Headings */
    h1, h2, h3 { color: #f5f5f7 !important; }
    .accent { color: #7c6af7; }

    /* Text area */
    .stTextArea textarea {
        background-color: #14141c !important;
        color: #e6e6ef !important;
        border: 1px solid #2a2a3a !important;
        border-radius: 10px !important;
    }
    .stTextArea textarea:focus { border-color: #7c6af7 !important; }

    /* Buttons */
    .stButton > button, .stDownloadButton > button {
        background-color: #7c6af7 !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 8px !important;
        padding: 0.6rem 1.6rem !important;
        font-weight: 600 !important;
    }
    .stButton > button:hover, .stDownloadButton > button:hover {
        background-color: #6a58e8 !important;
    }

    /* Metric cards */
    div[data-testid="stMetric"] {
        background-color: #14141c;
        border: 1px solid #2a2a3a;
        border-radius: 10px;
        padding: 14px;
    }
    div[data-testid="stMetricValue"] { color: #7c6af7; }
    div[data-testid="stMetricLabel"] { color: #a0a0b8; }

    /* Misc text */
    p, label, .stMarkdown { color: #c9c9d9; }
    footer, #MainMenu { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- header
st.markdown(
    "<h1>🔍 Meta Details <span class='accent'>Extractor</span></h1>",
    unsafe_allow_html=True,
)
st.markdown(
    "Extract **H1, Meta Title, Meta Description & Meta Keywords** from multiple "
    "URLs at once — with character counts for quick SEO checks."
)

# ---------------------------------------------------------------- extraction
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


def extract_meta(url: str) -> dict:
    """Fetch one URL and pull out its key on-page SEO elements."""
    row = {
        "URL": url,
        "Status": "",
        "H1": "",
        "Meta Title": "",
        "Title Length": 0,
        "Meta Description": "",
        "Description Length": 0,
        "Meta Keywords": "",
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        row["Status"] = str(resp.status_code)
        soup = BeautifulSoup(resp.text, "html.parser")

        h1 = soup.find("h1")
        row["H1"] = h1.get_text(strip=True) if h1 else "❌ Missing"

        title = soup.find("title")
        if title and title.get_text(strip=True):
            row["Meta Title"] = title.get_text(strip=True)
            row["Title Length"] = len(row["Meta Title"])
        else:
            row["Meta Title"] = "❌ Missing"

        desc = soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content"):
            row["Meta Description"] = desc["content"].strip()
            row["Description Length"] = len(row["Meta Description"])
        else:
            row["Meta Description"] = "❌ Missing"

        kws = soup.find("meta", attrs={"name": "keywords"})
        row["Meta Keywords"] = (
            kws["content"].strip() if kws and kws.get("content") else "❌ Missing"
        )
    except requests.exceptions.Timeout:
        row["Status"] = "Timeout"
    except requests.exceptions.RequestException as e:
        row["Status"] = f"Error: {type(e).__name__}"
    return row


def clean_urls(raw: str) -> list:
    urls = []
    for line in raw.splitlines():
        u = line.strip()
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        if u not in urls:
            urls.append(u)
    return urls


# ---------------------------------------------------------------- input
urls_input = st.text_area(
    "Enter URLs (one per line)",
    height=180,
    placeholder="https://example.com\nhttps://example.com/page-2\nexample.com/page-3",
)

col1, col2 = st.columns([1, 5])
with col1:
    run = st.button("🚀 Extract", use_container_width=True)

# ---------------------------------------------------------------- run
if run:
    urls = clean_urls(urls_input)
    if not urls:
        st.warning("Please enter at least one URL.")
        st.stop()
    if len(urls) > 200:
        st.warning("Limit is 200 URLs per run. Extracting the first 200.")
        urls = urls[:200]

    progress = st.progress(0, text="Fetching pages…")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(extract_meta, u): u for u in urls}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            results.append(fut.result())
            progress.progress(i / len(urls), text=f"Fetched {i}/{len(urls)} pages…")
    progress.empty()

    # keep original input order
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order[r["URL"]])
    df = pd.DataFrame(results)

    # ------------------------------------------------------------ summary
    ok = (df["Status"] == "200").sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("URLs Processed", len(df))
    m2.metric("Successful (200)", int(ok))
    m3.metric("Missing Titles", int((df["Meta Title"] == "❌ Missing").sum()))
    m4.metric("Missing Descriptions", int((df["Meta Description"] == "❌ Missing").sum()))

    st.dataframe(df, use_container_width=True, hide_index=True)

    # ------------------------------------------------------------ downloads
    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ Download CSV", csv, "meta_details.csv", "text/csv")

    xbuf = io.BytesIO()
    df.to_excel(xbuf, index=False, engine="openpyxl")
    st.download_button(
        "⬇️ Download Excel",
        xbuf.getvalue(),
        "meta_details.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# ---------------------------------------------------------------- footer
st.markdown(
    "<p style='text-align:center;color:#55556a;margin-top:3rem;'>"
    "Built by <span class='accent'>Mahalakshmi Marimuthu</span> · "
    "Digital Marketing Strategist & AI-Powered SEO Expert</p>",
    unsafe_allow_html=True,
)
