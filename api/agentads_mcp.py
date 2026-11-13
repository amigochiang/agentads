# AgentAds MCP server v2 - reads listings.csv, returns photos and contact paths,
# and saves REAL enquiries to enquiries.csv (and emails them if SMTP is set up).
# Horizon fix: mcp = build() at top level so Horizon can find the server object.
# Run for testing: python test_client.py     Real use: host it online (see README).
import csv, json, os, re, smtplib
from urllib.parse import quote_plus
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).parent
LISTINGS = HERE / "listings.csv"
ENQUIRIES = HERE / "enquiries.csv"
STOP = {"a","an","the","for","in","under","near","to","me","find","car","cars","room","rooms",
        "used","cheap","and","with","of","at","on","brisbane","i","want","need","looking"}

def _num(v, default=0.0):
    try: return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError: return default

def load_listings(category=None):
    if not LISTINGS.exists(): return []
    with open(LISTINGS, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("status") or "").strip().lower() == "live"]
    return [r for r in rows if category is None or (r.get("category") or "").strip().lower() == category]

def _location(r):
    place = " ".join(x for x in [r.get("suburb", ""), r.get("postcode", ""), r.get("city", ""), "Australia"] if x).strip()
    return {
        "area": " ".join(x for x in [r.get("suburb", ""), r.get("city", ""), r.get("postcode", "")] if x),
        "map_link": "https://www.google.com/maps/search/?api=1&query=" + quote_plus(place),
        "viewing": (r.get("viewing_info") or "").strip()
                   or "Viewing by appointment. A public meeting spot or the exact address is shared once a viewing is confirmed.",
        "note": "Map link shows the suburb only, not the seller's home.",
    }

def public_view(r, score=None):
    photos = [p.strip() for p in (r.get("photo_urls") or "").split("|") if p.strip().startswith("https://")]
    bid = _num(r.get("bid"))
    out = {
        "id": r["id"], "title": r["title"], "price": _num(r["price"]), "suburb": r.get("suburb", ""),
        "city": r.get("city", ""), "postcode": r.get("postcode", ""), "description": r.get("description", ""),
        "location": _location(r),
        "photos": photos,
        "photos_note": "" if photos else "No photos supplied yet. Ask the seller via send_enquiry.",
        "listing_url": r.get("listing_url", ""),
        "label": "Sponsored" if bid > 0 else "Organic",
        "how_to_contact": "Call send_enquiry with this listing id, your name, how the seller can reply, and a message."
                          + (" Seller's public contact: " + r["public_contact"] if r.get("public_contact") else ""),
    }
    if r.get("kms"): out["kms"] = int(_num(r["kms"]))
    if r.get("rating"): out["rating"] = _num(r["rating"])
    if score is not None: out["score"] = round(score, 3)
    return out

def rank(rows, query, max_price, location=""):
    words = [w for w in re.findall(r"[\w\u4e00-\u9fff]+", (query or "").lower()) if w not in STOP and not w.isdigit()]
    results = []
    for r in rows:
        if _num(r["price"]) > max_price: continue
        if location and location.lower() not in (r.get("city", "") + " " + r.get("suburb", "") + " " + r.get("postcode", "")).lower(): continue
        text = " ".join([r.get("title", ""), r.get("description", ""), r.get("suburb", "")]).lower()
        match = (sum(w in text for w in words) / len(words)) if words else 1.0
        relevance = 0.5 + 0.5 * match
        rating_factor = _num(r.get("rating")) / 5 if r.get("rating") else 0.8
        boost = 1 + min(_num(r.get("bid")), 5) * 0.05
        results.append((relevance * rating_factor * boost, r))
    results.sort(key=lambda x: x[0], reverse=True)
    return [public_view(r, s) for s, r in results[:3]]

ENQ_COLS = ["id", "time", "listing_id", "buyer_name", "reply_to", "message", "status", "note"]
STATUSES = {"new", "replied", "spam", "closed"}

def read_enquiries():
    if not ENQUIRIES.exists(): return []
    with open(ENQUIRIES, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for i, r in enumerate(rows):
        r["id"] = r.get("id") or f"E{i+1}"
        r["status"] = r.get("status") or "new"
    return rows

def write_enquiries(rows):
    with open(ENQUIRIES, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=ENQ_COLS); w.writeheader()
        for r in rows: w.writerow({k: r.get(k, "") for k in ENQ_COLS})

def save_enquiry(listing_id, buyer_name, reply_to, message):
    rows_l = {r["id"]: r for r in load_listings()}
    if listing_id not in rows_l: return {"status": "error", "message": "Listing not found or no longer live."}
    if not reply_to.strip() or not message.strip(): return {"status": "error", "message": "reply_to and message are required."}
    rows = read_enquiries()
    e = {"id": f"E{len(rows)+1}", "time": datetime.now().isoformat(timespec="seconds"), "listing_id": listing_id,
         "buyer_name": buyer_name[:80], "reply_to": reply_to[:120], "message": message[:1500], "status": "new", "note": ""}
    rows.append(e); write_enquiries(rows)
    emailed = _email(rows_l[listing_id], e)
    return {"status": "saved", "enquiry_id": e["id"], "emailed_to_seller": emailed,
            "message": "Enquiry recorded. The seller has NOT confirmed anything yet; they will reply to the contact you gave."
                       + ("" if emailed else " (Seller notification email is not set up: seller checks enquiries.csv.)")}

def _email(listing, e):
    host, user, pw = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    to = (listing.get("notify_email") or os.environ.get("NOTIFY_EMAIL") or "").strip()
    if not (host and user and pw and to): return False
    try:
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = f"New enquiry: {listing['title']}", user, to
        m.set_content(f"From: {e['buyer_name']}\nReply to: {e['reply_to']}\n\n{e['message']}")
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls(); s.login(user, pw); s.send_message(m)
        return True
    except Exception:
        return False

def update_enquiry(enquiry_id, status, note=""):
    if status not in STATUSES: return {"status": "error", "message": "status must be one of: " + ", ".join(sorted(STATUSES))}
    rows = read_enquiries()
    for r in rows:
        if r["id"] == enquiry_id:
            r["status"], r["note"] = status, note[:300]; write_enquiries(rows)
            return {"status": "updated", "enquiry_id": enquiry_id, "new_status": status}
    return {"status": "error", "message": "Enquiry not found."}

def mark_sold(listing_id):
    if not LISTINGS.exists(): return {"status": "error", "message": "listings.csv not found."}
    with open(LISTINGS, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f); fields, rows = rd.fieldnames, list(rd)
    hit = False
    for r in rows:
        if r["id"] == listing_id: r["status"], hit = "sold", True
    if not hit: return {"status": "error", "message": "Listing not found."}
    with open(LISTINGS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    return {"status": "updated", "listing_id": listing_id, "new_status": "sold",
            "reminder": "Now take the ad down on every site where it was posted."}

def stale_listings(days=7):
    counts = {}
    for e in read_enquiries(): counts[e["listing_id"]] = counts.get(e["listing_id"], 0) + 1
    out = []
    for r in load_listings():
        try: age = (datetime.now() - datetime.strptime(r.get("posted", ""), "%Y-%m-%d")).days
        except ValueError: continue
        if age >= days and counts.get(r["id"], 0) == 0:
            out.append({"id": r["id"], "title": r["title"], "price": _num(r["price"]), "days_live": age, "enquiries": 0})
    return out

def build():
    from fastmcp import FastMCP
    mcp = FastMCP("AgentAds")

    @mcp.tool()
    def search_used_cars(query: str, max_price: int = 20000, location: str = "Brisbane") -> str:
        """Search live used-car listings. Sponsored results are labelled. Use when the user wants to buy a used car."""
        return json.dumps({"offers": rank(load_listings("car"), query, max_price, location)}, ensure_ascii=False, indent=2)

    @mcp.tool()
    def search_room_rentals(query: str, max_price_week: int = 400, location: str = "Brisbane") -> str:
        """Search live room and rental listings (price is per week). Use when the user wants a room or place to rent."""
        return json.dumps({"offers": rank(load_listings("room"), query, max_price_week, location)}, ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_listing(id: str) -> str:
        """Get the full details, photos and contact path of one listing."""
        for r in load_listings():
            if r["id"] == id: return json.dumps(public_view(r), ensure_ascii=False, indent=2)
        return json.dumps({"status": "error", "message": "Listing not found."})

    @mcp.tool()
    def send_enquiry(listing_id: str, buyer_name: str, reply_to: str, message: str) -> str:
        """Send an enquiry to the seller. Only call after the user has confirmed the message and the contact they want the seller to reply to."""
        return json.dumps(save_enquiry(listing_id, buyer_name, reply_to, message), ensure_ascii=False)

    if os.environ.get("AGENTADS_OWNER") == "1":
        titles = lambda: {r["id"]: r["title"] for r in load_listings()}

        @mcp.tool()
        def list_enquiries(status: str = "new") -> str:
            """OWNER: list enquiries (status new, replied, spam, closed). Each includes the listing title."""
            t = titles()
            rows = [dict(r, listing_title=t.get(r["listing_id"], "")) for r in read_enquiries() if r["status"] == status]
            return json.dumps(rows, ensure_ascii=False, indent=2)

        @mcp.tool()
        def set_enquiry_status(enquiry_id: str, status: str, note: str = "") -> str:
            """OWNER: set an enquiry to new, replied, spam or closed, with a short note."""
            return json.dumps(update_enquiry(enquiry_id, status, note), ensure_ascii=False)

        @mcp.tool()
        def mark_listing_sold(listing_id: str) -> str:
            """OWNER: mark a listing sold so it disappears from search."""
            return json.dumps(mark_sold(listing_id), ensure_ascii=False)

        @mcp.tool()
        def stale_listing_report(days: int = 7) -> str:
            """OWNER: live listings older than N days that have had no enquiries."""
            return json.dumps(stale_listings(days), ensure_ascii=False, indent=2)
    return mcp

# --- HORIZON FIX: expose server object at top level ---
mcp = build()

if __name__ == "__main__":
    if os.environ.get("AGENTADS_HTTP") == "1":
        if os.environ.get("AGENTADS_OWNER") == "1":
            raise SystemExit("Refusing to start: never run owner tools on an online server. Remove AGENTADS_OWNER.")
        build().run(transport="http", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
    else:
        build().run()
