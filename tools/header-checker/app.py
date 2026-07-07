"""
Header Tags Extractor & Checker
Extract H1–H6 tags from multiple URLs, view heading structure & flag issues.
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
    page_title="Header Tags Extractor & Checker",
    page_icon="🏷️",
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

    /* Expanders */
    div[data-testid="stExpander"] {
        background-color: #12121a;
        border: 1px solid #2a2a3a;
        border-radius: 10px;
    }

    /* Heading tree */
    .htree { font-family: 'DM Mono', 'Courier New', monospace; font-size: 0.85rem; line-height: 2; }
    .htree .lvl { color: #7c6af7; font-weight: 700; }
    .htree .txt { color: #c9c9d9; }

    /* Misc text */
    p, label, .stMarkdown { color: #c9c9d9; }
    footer, #MainMenu { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- header
st.markdown(
    "<h1>🏷️ Header Tags <span class='accent'>Extractor & Checker</span></h1>",
    unsafe_allow_html=True,
)
st.markdown(
    "Pull every **H1–H6 tag** from your pages in order, see the heading structure "
    "at a glance, and catch issues like **missing H1s, multiple H1s and skipped levels**."
)

# ---------------------------------------------------------------- extraction
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


def analyze_headings(headings: list) -> dict:
    """Run SEO checks on an ordered list of (level, text) heading tuples."""
    h1_count = sum(1 for lvl, _ in headings if lvl == 1)
    skips = []
    prev = None
    for lvl, _ in headings:
        if prev is not None and lvl > prev + 1:
            skips.append(f"H{prev} → H{lvl}")
        prev = lvl
    issues = []
    if h1_count == 0:
        issues.append("Missing H1")
    if h1_count > 1:
        issues.append(f"Multiple H1s ({h1_count})")
    if skips:
        issues.append("Skipped levels: " + ", ".join(skips))
    return {"h1_count": h1_count, "skips": skips, "issues": issues}


def extract_headings(url: str) -> dict:
    """Fetch one URL and pull out its H1–H6 tags in document order."""
    row = {"URL": url, "Status": "", "Headings": [], "Checks": {}}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        row["Status"] = str(resp.status_code)
        soup = BeautifulSoup(resp.text, "html.parser")
        headings = [
            (int(tag.name[1]), tag.get_text(strip=True))
            for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        ]
        row["Headings"] = headings
        row["Checks"] = analyze_headings(headings)
    except requests.exceptions.Timeout:
        row["Status"] = "Timeout"
        row["Checks"] = {"h1_count": 0, "skips": [], "issues": ["Could not fetch"]}
    except requests.exceptions.RequestException as e:
        row["Status"] = f"Error: {type(e).__name__}"
        row["Checks"] = {"h1_count": 0, "skips": [], "issues": ["Could not fetch"]}
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
        futures = {pool.submit(extract_headings, u): u for u in urls}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            results.append(fut.result())
            progress.progress(i / len(urls), text=f"Fetched {i}/{len(urls)} pages…")
    progress.empty()

    # keep original input order
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order[r["URL"]])

    # ------------------------------------------------------------ summary df
    summary_rows = []
    for r in results:
        c = r["Checks"]
        level_counts = {lvl: 0 for lvl in range(1, 7)}
        for lvl, _ in r["Headings"]:
            level_counts[lvl] += 1
        summary_rows.append(
            {
                "URL": r["URL"],
                "Status": r["Status"],
                "Total Headings": len(r["Headings"]),
                "H1": level_counts[1],
                "H2": level_counts[2],
                "H3": level_counts[3],
                "H4": level_counts[4],
                "H5": level_counts[5],
                "H6": level_counts[6],
                "Missing H1": "❌ Yes" if "Missing H1" in " ".join(c.get("issues", [])) else "✅ No",
                "Multiple H1s": "❌ Yes" if c.get("h1_count", 0) > 1 else "✅ No",
                "Skipped Levels": ", ".join(c.get("skips", [])) or "✅ None",
                "Issues": "; ".join(c.get("issues", [])) or "✅ All good",
            }
        )
    df = pd.DataFrame(summary_rows)

    ok = (df["Status"] == "200").sum()
    pages_with_issues = (df["Issues"] != "✅ All good").sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("URLs Processed", len(df))
    m2.metric("Successful (200)", int(ok))
    m3.metric("Pages With Issues", int(pages_with_issues))
    m4.metric("Clean Pages", int(len(df) - pages_with_issues))

    st.markdown("### 📋 Issues Summary")
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ------------------------------------------------------------ heading trees
    st.markdown("### 🌳 Heading Structure")
    for r in results:
        issues = r["Checks"].get("issues", [])
        flag = "⚠️ " if issues else "✅ "
        with st.expander(f"{flag}{r['URL']}  ·  {len(r['Headings'])} headings"):
            if issues:
                st.warning(" · ".join(issues))
            if not r["Headings"]:
                st.markdown("_No heading tags found on this page._")
            else:
                lines = []
                for lvl, txt in r["Headings"]:
                    indent = "&nbsp;" * 6 * (lvl - 1)
                    safe = (txt or "(empty)").replace("<", "&lt;").replace(">", "&gt;")
                    lines.append(
                        f"{indent}<span class='lvl'>H{lvl}</span> "
                        f"<span class='txt'>{safe}</span>"
                    )
                st.markdown(
                    "<div class='htree'>" + "<br>".join(lines) + "</div>",
                    unsafe_allow_html=True,
                )

    # ------------------------------------------------------------ downloads
    # one row per URL: all headings of each level grouped into one column
    flat_rows = []
    for r in results:
        by_level = {lvl: [] for lvl in range(1, 7)}
        for lvl, txt in r["Headings"]:
            by_level[lvl].append(txt or "(empty)")
        flat_rows.append(
            {
                "URL": r["URL"],
                "H1": " | ".join(by_level[1]),
                "H2": " | ".join(by_level[2]),
                "H3": " | ".join(by_level[3]),
                "H4": " | ".join(by_level[4]),
                "H5": " | ".join(by_level[5]),
                "H6": " | ".join(by_level[6]),
            }
        )
    flat_df = pd.DataFrame(flat_rows) if flat_rows else pd.DataFrame(
        columns=["URL", "H1", "H2", "H3", "H4", "H5", "H6"]
    )

    d1, d2, d3 = st.columns([1, 1, 4])
    with d1:
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ Summary CSV", csv, "header_check_summary.csv", "text/csv")
    with d2:
        xbuf = io.BytesIO()
        with pd.ExcelWriter(xbuf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Summary")
            flat_df.to_excel(writer, index=False, sheet_name="All Headings")
        st.download_button(
            "⬇️ Excel (Summary + Headings)",
            xbuf.getvalue(),
            "header_tags_report.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

# ---------------------------------------------------------------- footer
st.markdown(
    "<p style='text-align:center;color:#55556a;margin-top:3rem;'>"
    "Built by <span class='accent'>Mahalakshmi Marimuthu</span> · "
    "Digital Marketing Strategist & AI-Powered SEO Expert</p>",
    unsafe_allow_html=True,
)
