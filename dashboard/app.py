"""Pharma OS dashboard — for staff with a screen.

Deliberately secondary to WhatsApp. The manager should never *need* to open this;
it exists for the three jobs a chat window is genuinely bad at:
  1. the pharmacist verification queue (needs the script image beside the extraction)
  2. editing GRN lines in bulk
  3. scanning a long expiry worklist

Run:  streamlit run app.py
"""
import base64
import hmac
import os
import uuid
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import psycopg
import streamlit as st
from psycopg.rows import dict_row
from supabase import create_client

# One brand source for the whole app. brand/ is generated from brand/logo.svg by
# brand/make_assets.py, so the dashboard tab icon, the sign-in mark and the PDF
# letterhead can never drift apart.
BRAND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "brand")
ICON = os.path.join(BRAND, "icon-192.png")
ORANGE = "#FF7A00"

st.set_page_config(page_title="Pharma OS", page_icon=ICON, layout="wide")

DATABASE_URL = os.environ["DATABASE_URL"]
PID = os.environ["PHARMACY_ID"]
SB_URL = os.environ["SUPABASE_URL"]
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
# No default, and no "empty means no gate". This surface holds prescription images,
# patient names and the service_role key. An unconfigured deploy must refuse to run,
# not silently open — that is the failure mode you never notice.
APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
BUCKET_RX = os.getenv("BUCKET_RX", "prescriptions")
BUCKET_INV = os.getenv("BUCKET_INVOICES", "invoices")


# ------------------------------------------------------------------ plumbing
@st.cache_resource
def _sb():
    return create_client(SB_URL, SB_KEY)


def q(sql, params=None) -> list[dict]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def ex(sql, params=None):
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def img_url(bucket: str, path: str, secs: int = 3600) -> str | None:
    if not path:
        return None
    try:
        r = _sb().storage.from_(bucket).create_signed_url(path, secs)
        return r.get("signedURL") or r.get("signedUrl")
    except Exception:
        return None


def wp(pieces, pack_size) -> str:
    ps = max(int(pack_size or 1), 1)
    w, p = divmod(int(pieces or 0), ps)
    return f"{w}W{p}P"


def kes(v) -> str:
    try:
        return f"KES {float(v):,.0f}"
    except (TypeError, ValueError):
        return "KES 0"


def _api():
    """Import the api/ package so mutations go through business logic rather than
    raw UPDATEs. A bare UPDATE from the UI notifies nobody and records no actor."""
    import sys
    api_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)


def _set_order_status(order_id, new_status: str, actor: dict,
                      phone: str | None, code: str | None):
    """Status change + customer notification + who did it."""
    _api()
    from wa import send_text
    ex("update orders set status=%s where id=%s", (new_status, order_id))
    # A zero-delta movement is an audit breadcrumb, not a stock change: it records
    # who moved the order and when without touching batch quantities, so the
    # ledger-vs-batches invariant still holds.
    ex("""insert into stock_movements (pharmacy_id, batch_id, delta_pieces, reason,
                actor_staff, ref_table, ref_id, note)
          select %s, l.batch_id, 0, 'adjust', %s, 'orders', %s, %s
            from order_lines l where l.order_id = %s and l.batch_id is not null
            limit 1""",
       (PID, actor["id"], order_id, f"status -> {new_status} by {actor['name']}",
        order_id))
    if phone:
        if new_status == "dispatched":
            send_text(phone, "🛵 Your order is on the way."
                             + (f" Give the rider code *{code}*." if code else ""))
        elif new_status == "delivered":
            send_text(phone, "✅ Delivered. Thank you for choosing us. "
                             "Reply *POINTS* to see your loyalty balance.")


def zoomable(url: str, height: int = 460):
    """Pinch/scroll-zoom + drag for prescription images.

    Streamlit's st.image has no zoom, and zoom is the single most important control
    when reading a handwritten script. 30 lines of HTML beats rejecting a whole
    prescription because one dose was unreadable.
    """
    import streamlit.components.v1 as components
    components.html(f"""
    <div style="position:relative;overflow:hidden;height:{height}px;
                background:#111;border-radius:10px;cursor:grab" id="vp">
      <img id="im" src="{url}" style="position:absolute;transform-origin:0 0;
           max-width:none;user-select:none" draggable="false"/>
      <div style="position:absolute;bottom:8px;right:10px;background:rgba(0,0,0,.65);
                  color:#fff;font:11px system-ui;padding:4px 9px;border-radius:12px">
        scroll to zoom · drag to pan · double-click to reset
      </div>
    </div>
    <script>
    (function(){{
      const vp=document.getElementById('vp'), im=document.getElementById('im');
      let s=1,x=0,y=0,drag=false,px=0,py=0;
      function fit(){{ s=Math.min(vp.clientWidth/im.naturalWidth,
                                 vp.clientHeight/im.naturalHeight)||1;
                       x=(vp.clientWidth-im.naturalWidth*s)/2; y=0; draw(); }}
      function draw(){{ im.style.transform=`translate(${{x}}px,${{y}}px) scale(${{s}})`; }}
      im.onload=fit;
      vp.addEventListener('wheel',e=>{{ e.preventDefault();
        const r=vp.getBoundingClientRect(), mx=e.clientX-r.left, my=e.clientY-r.top;
        const f=e.deltaY<0?1.15:1/1.15, ns=Math.max(.1,Math.min(s*f,12));
        x=mx-(mx-x)*(ns/s); y=my-(my-y)*(ns/s); s=ns; draw(); }},{{passive:false}});
      vp.addEventListener('mousedown',e=>{{drag=true;px=e.clientX;py=e.clientY;
        vp.style.cursor='grabbing';}});
      window.addEventListener('mouseup',()=>{{drag=false;vp.style.cursor='grab';}});
      window.addEventListener('mousemove',e=>{{ if(!drag)return;
        x+=e.clientX-px; y+=e.clientY-py; px=e.clientX; py=e.clientY; draw(); }});
      vp.addEventListener('dblclick',fit);
      let d0=null;
      vp.addEventListener('touchstart',e=>{{ if(e.touches.length===2)
        d0=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,
                      e.touches[0].clientY-e.touches[1].clientY); }});
      vp.addEventListener('touchmove',e=>{{ if(e.touches.length===2&&d0){{
        e.preventDefault();
        const d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,
                           e.touches[0].clientY-e.touches[1].clientY);
        s=Math.max(.1,Math.min(s*(d/d0),12)); d0=d; draw(); }} }},{{passive:false}});
    }})();
    </script>
    """, height=height + 10)


# ------------------------------------------------------------------ auth
if not APP_PASSWORD:
    st.error("DASHBOARD_PASSWORD is not set. Refusing to start without a gate.")
    st.stop()

if not st.session_state.get("authed"):
    from signin import sign_in_page
    sign_in_page(ICON, ORANGE, APP_PASSWORD, hmac)
    st.stop()

# ------------------------------------------------------------------ tenant
# PHARMACY_ID from the environment is the DEFAULT, not a hard wire, so one dashboard
# can onboard and then switch between pharmacies. Every query below still filters on
# PID explicitly -- see onboarding.py for why that is discipline rather than isolation.
_pharmacies = q("select id, name from pharmacies order by created_at")
if not _pharmacies:
    # Nothing exists yet. Onboarding has to work from a genuinely empty database or
    # there is no way in.
    from onboarding import first_run
    first_run(q, ex)
    st.stop()

if st.session_state.get("pid") not in [str(p["id"]) for p in _pharmacies]:
    st.session_state["pid"] = str(PID) if str(PID) in [str(p["id"]) for p in _pharmacies] \
        else str(_pharmacies[0]["id"])
PID = st.session_state["pid"]

# Acting user — every clinical approval must be attributable to a real person
staff = q("""select id, name, role, ppb_reg_no from staff
              where pharmacy_id=%s and is_active
              order by case role when 'pharmacist' then 0 when 'owner' then 1
                                 when 'manager' then 2 else 3 end, name""", (PID,))

with st.sidebar:
    # The mark, not an emoji. Rendered inline as base64 rather than st.image so the
    # logo and wordmark sit on one line at a controlled size — st.image in a sidebar
    # column would stack them and take three times the vertical space.
    _logo_b64 = base64.b64encode(open(os.path.join(BRAND, "icon-192.png"), "rb")
                                 .read()).decode()
    st.markdown(
        f"""<div style="display:flex;align-items:center;gap:10px;margin:0 0 4px">
              <img src="data:image/png;base64,{_logo_b64}" width="30" height="30"/>
              <span style="font-size:26px;font-weight:700;letter-spacing:-0.4px">
                Pharma OS</span>
            </div>""",
        unsafe_allow_html=True)
    if len(_pharmacies) > 1:
        plabels = {p["name"]: str(p["id"]) for p in _pharmacies}
        chosen = st.selectbox("Pharmacy", list(plabels),
                             index=[str(p["id"]) for p in _pharmacies].index(PID))
        if plabels[chosen] != PID:
            st.session_state["pid"] = plabels[chosen]
            st.rerun()
    else:
        st.caption(_pharmacies[0]["name"])

    PAGES = ["Verification queue", "Receiving", "Stock", "Expiry",
             "Purchase orders", "Orders", "Suppliers", "Manual upload",
             "Setup", "System"]

    if not staff:
        # A pharmacy with no staff cannot do anything: WhatsApp only answers numbers
        # in `staff`, and every approval needs an attributable actor. Send the user
        # straight to Setup instead of dead-ending with an error.
        st.warning("No staff yet — add the owner's number to begin.")
        me = {"id": None, "name": "Setup", "role": "owner", "ppb_reg_no": None}
        page = "Setup"
    else:
        labels = {f"{s['name']} · {s['role']}": s for s in staff}
        me = labels[st.selectbox("Signed in as", list(labels))]
        st.caption(f"PPB reg: {me['ppb_reg_no'] or '—'}")
        st.divider()
        page = st.radio("", PAGES, label_visibility="collapsed")


# ============================================================ verification queue
if page == "Verification queue":
    st.header("Prescription verification")
    st.caption("A licensed pharmacist must approve before any price reaches the customer. "
               "Your name and PPB number are written to the record.")

    rows = q("""select p.*, c.phone, c.name as customer_name
                  from prescriptions p join customers c on c.id = p.customer_id
                 where p.pharmacy_id=%s and p.status='pending_verification'
                 order by p.created_at""", (PID,))
    if not rows:
        st.success("Queue is empty.")
    for r in rows:
        with st.container(border=True):
            left, right = st.columns([1, 1])
            with left:
                url = img_url(BUCKET_RX, r["image_path"])
                if url:
                    zoomable(url)
                    st.caption("Zoom in and read every line before approving.")
                else:
                    st.warning("Image unavailable")
            with right:
                st.markdown(f"**{r['customer_name'] or r['phone']}** · {r['phone']}")
                st.caption(f"Received {r['created_at']:%d %b %H:%M} · "
                           f"confidence {float(r['confidence'] or 0):.2f}")
                if r["flags"]:
                    st.warning("Flags: " + ", ".join(r["flags"]))
                st.write(f"Patient: {r['patient_name'] or '—'}")
                st.write(f"Prescriber: {r['prescriber_name'] or '—'} "
                         f"(reg {r['prescriber_reg'] or '—'})")
                st.write(f"Issued: {r['issued_date'] or '—'}")
                st.markdown("**Extracted items — check every line against the image**")
                st.dataframe(pd.DataFrame(r["extracted"] or []),
                             use_container_width=True, hide_index=True)

                if me["role"] not in ("pharmacist", "owner", "manager"):
                    st.error("Your role cannot verify prescriptions.")
                else:
                    c1, c2 = st.columns(2)
                    if c1.button("✅ Approve & send quote", key=f"a{r['id']}",
                                 type="primary", use_container_width=True):
                        import sys
                        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
                        from rx import pharmacist_approve
                        pharmacist_approve(str(r["id"]), str(me["id"]))
                        st.success("Approved. Quote sent to the customer.")
                        st.rerun()
                    reason = c2.text_input("Rejection reason", key=f"rr{r['id']}",
                                           placeholder="e.g. illegible, expired script")
                    if c2.button("❌ Reject", key=f"r{r['id']}", use_container_width=True):
                        if not reason.strip():
                            st.error("A reason is required.")
                        else:
                            import sys
                            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
                            from rx import pharmacist_reject
                            pharmacist_reject(str(r["id"]), str(me["id"]), reason.strip())
                            st.warning("Rejected and customer notified.")
                            st.rerun()


# ============================================================ receiving
elif page == "Receiving":
    st.header("Goods receiving")
    tab_review, tab_history = st.tabs(["Needs review", "History"])

    with tab_review:
        grns = q("""select g.*, s.name as supplier from grns g
                     left join suppliers s on s.id=g.supplier_id
                    where g.pharmacy_id=%s and g.status='needs_review'
                    order by g.created_at desc""", (PID,))
        if not grns:
            st.success("Nothing awaiting review.")
        for g in grns:
            with st.expander(f"{g['supplier'] or '?'} · {g['invoice_no'] or 'no number'} · "
                             f"{kes(g['net_total'] or g['parsed_total'])}", expanded=True):
                c1, c2 = st.columns([1, 2])
                with c1:
                    for p in (g["images"] or [])[:3]:
                        u = img_url(BUCKET_INV, p)
                        if u:
                            st.image(u, use_container_width=True)
                with c2:
                    parsed = float(g["parsed_total"] or 0)
                    printed = float(g["net_total"]) if g["net_total"] is not None else None
                    if printed is not None and abs(printed - parsed) > 1:
                        st.error(f"Our sum {kes(parsed)} vs printed {kes(printed)} — "
                                 f"difference {kes(abs(printed - parsed))}")
                    else:
                        st.success(f"Totals reconcile: {kes(parsed)}")

                    lines = q("""select l.*, p.name as product_name, p.pack_size
                                   from grn_lines l left join products p on p.id=l.product_id
                                  where l.grn_id=%s order by l.line_no""", (g["id"],))
                    df = pd.DataFrame([{
                        "line": l["line_no"],
                        "product": l["product_name"] or f"⚠️ {l['raw_description']}",
                        "batch": l["batch_no"],
                        "expiry": l["expiry_date"],
                        "qty (pcs)": l["qty_invoiced_pieces"],
                        "counted": l["qty_counted_pieces"],
                        "price": l["unit_price"],
                        "flags": ", ".join(l["flags"] or []),
                    } for l in lines])
                    edited = st.data_editor(
                        df, use_container_width=True, hide_index=True,
                        disabled=["line", "product", "flags"],
                        key=f"ed{g['id']}",
                    )
                    if st.button("💾 Save line edits", key=f"s{g['id']}"):
                        for _, row in edited.iterrows():
                            ex("""update grn_lines
                                     set batch_no=%s, expiry_date=%s,
                                         qty_counted_pieces=%s,
                                         flags=array_remove(array_remove(flags,'missing_batch'),
                                                            'missing_expiry')
                                   where grn_id=%s and line_no=%s""",
                               (row["batch"] or None,
                                pd.to_datetime(row["expiry"]).date()
                                if pd.notna(row["expiry"]) else None,
                                int(row["counted"]) if pd.notna(row["counted"]) else None,
                                g["id"], int(row["line"])))
                        st.success("Saved.")
                        st.rerun()

                    unmatched = [l for l in lines if not l["product_id"]]
                    if unmatched:
                        st.warning(f"{len(unmatched)} line(s) not linked to a product. "
                                   "Link or create them before receiving.")
                        for l in unmatched:
                            cc1, cc2 = st.columns([3, 1])
                            pick = cc1.selectbox(
                                f"Line {l['line_no']}: {l['raw_description']}",
                                ["— create as new product —"] +
                                [f"{p['name']}" for p in q(
                                    "select name from products where pharmacy_id=%s "
                                    "order by name limit 400", (PID,))],
                                key=f"pk{l['id']}")
                            if cc2.button("Link", key=f"lk{l['id']}"):
                                if pick.startswith("—"):
                                    ex("""insert into products
                                            (pharmacy_id, legacy_code, name, pack_size, cost_price)
                                          values (%s,%s,%s,1,%s)
                                          on conflict do nothing""",
                                       (PID, l["raw_code"] or f"NEW-{l['line_no']}",
                                        l["raw_description"], l["unit_price"]))
                                    target = l["raw_description"]
                                else:
                                    target = pick
                                ex("""update grn_lines set product_id =
                                        (select id from products where pharmacy_id=%s
                                          and name=%s limit 1),
                                        flags=array_remove(flags,'unmatched_product')
                                      where id=%s""", (PID, target, l["id"]))
                                st.rerun()
                    elif st.button("✅ Receive into stock", key=f"ap{g['id']}",
                                   type="primary"):
                        import sys
                        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
                        from grn import approve
                        approve(str(g["id"]), dict(me), "")
                        st.success("Received.")
                        st.rerun()

    with tab_history:
        hist = q("""select g.invoice_no, s.name as supplier, g.invoice_date, g.net_total,
                           g.discrepancy_note, st.name as approved_by, g.approved_at
                      from grns g
                      left join suppliers s on s.id=g.supplier_id
                      left join staff st on st.id=g.approved_by
                     where g.pharmacy_id=%s and g.status='approved'
                     order by g.approved_at desc limit 100""", (PID,))
        st.dataframe(pd.DataFrame(hist), use_container_width=True, hide_index=True)


# ============================================================ stock
elif page == "Stock":
    st.header("Stock on hand")
    search = st.text_input("Search", placeholder="amoxil, prenor, TABS0292")
    rows = q("""select name, legacy_code, pack_size, qty_pieces, whole_packs,
                       earliest_expiry, reorder_level_pieces, sell_price
                  from v_stock_on_hand
                 where pharmacy_id=%s and (%s = '' or name ilike %s or legacy_code ilike %s)
                 order by name limit 500""",
             (PID, search, f"%{search}%", f"%{search}%"))
    df = pd.DataFrame([{
        "Code": r["legacy_code"], "Product": r["name"],
        "On hand": wp(r["qty_pieces"], r["pack_size"]),
        "Pieces": r["qty_pieces"],
        "Reorder at": r["reorder_level_pieces"],
        "Earliest expiry": r["earliest_expiry"],
        "Price": r["sell_price"],
    } for r in rows])
    st.dataframe(df, use_container_width=True, hide_index=True, height=560)


# ============================================================ expiry
elif page == "Expiry":
    st.header("Expiry worklist")
    days = st.slider("Within days", min_value=30, max_value=365, value=120, step=30)
    rows = q("""select name, batch_no, expiry_date, qty_pieces, value_at_risk, days_left
                  from v_expiry_risk where pharmacy_id=%s and days_left <= %s
                 order by expiry_date""", (PID, days))
    total = sum(float(r["value_at_risk"] or 0) for r in rows)
    c1, c2 = st.columns(2)
    c1.metric("Batches at risk", len(rows))
    c2.metric("Value at risk", kes(total))
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=520)


# ============================================================ purchase orders
elif page == "Purchase orders":
    st.header("Purchase orders")
    pos = q("""select po.*, s.name as supplier, s.phone
                 from purchase_orders po join suppliers s on s.id=po.supplier_id
                where po.pharmacy_id=%s order by po.created_at desc limit 50""", (PID,))
    for po in pos:
        with st.expander(f"{po['supplier']} · {po['status']} · "
                         f"{kes(po['total_estimate'])} · {po['created_at']:%d %b}"):
            lines = q("""select p.name, p.pack_size, l.qty_pieces, l.unit_cost, l.rationale
                           from po_lines l join products p on p.id=l.product_id
                          where l.po_id=%s""", (po["id"],))
            st.dataframe(pd.DataFrame([{
                "Product": l["name"],
                "Order": wp(l["qty_pieces"], l["pack_size"]),
                "Unit cost": l["unit_cost"],
                "Why": l["rationale"],
            } for l in lines]), use_container_width=True, hide_index=True)
            if po["status"] == "awaiting_approval":
                if st.button("✅ Approve & WhatsApp to supplier", key=f"po{po['id']}",
                             type="primary"):
                    import sys
                    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
                    from wa import send_text
                    body = "\n".join(f"• {l['name']} — {wp(l['qty_pieces'], l['pack_size'])}"
                                     for l in lines)
                    send_text(po["phone"],
                              f"Purchase order from our pharmacy:\n\n{body}\n\n"
                              f"Please confirm availability and delivery date.")
                    ex("""update purchase_orders set status='sent', approved_by=%s,
                             approved_at=now(), sent_at=now() where id=%s""",
                       (me["id"], po["id"]))
                    st.success("Sent.")
                    st.rerun()


# ============================================================ orders
elif page == "Orders":
    st.header("Customer orders")
    rows = q("""select o.id, o.status, o.total, o.delivery_code, o.created_at,
                       c.phone, c.name
                  from orders o join customers c on c.id=o.customer_id
                 where o.pharmacy_id=%s order by o.created_at desc limit 100""", (PID,))
    for r in rows:
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        c1.write(f"**{str(r['id'])[:8].upper()}** · {r['name'] or r['phone']}")
        c2.write(kes(r["total"]))
        c3.write(r["status"])
        # Status changes go through api/ so the customer is notified and the change
        # is attributable. A bare UPDATE from the UI notifies nobody and leaves no
        # record of who dispatched.
        if r["status"] == "paid":
            if c4.button("Mark dispatched", key=f"d{r['id']}"):
                with st.spinner("Dispatching..."):
                    try:
                        _set_order_status(r["id"], "dispatched", me, r["phone"],
                                          r["delivery_code"])
                        st.success("Dispatched and customer notified.")
                    except Exception as e:
                        st.error(f"Failed: {e}")
                st.rerun()
        elif r["status"] == "dispatched":
            if c4.button("Mark delivered", key=f"dl{r['id']}"):
                with st.spinner("Confirming..."):
                    try:
                        _set_order_status(r["id"], "delivered", me, r["phone"], None)
                        st.success("Marked delivered.")
                    except Exception as e:
                        st.error(f"Failed: {e}")
                st.rerun()


# ============================================================ suppliers
elif page == "Suppliers":
    st.header("Suppliers")
    st.caption("This list is the answer to 'what happens when her phone breaks'.")
    rows = q("""select id, code, name, phone, alt_phone, rep_name, email, mpesa_paybill
                  from suppliers where pharmacy_id=%s order by name""", (PID,))
    edited = st.data_editor(pd.DataFrame(rows), use_container_width=True,
                            hide_index=True, num_rows="dynamic", disabled=["id"])
    if st.button("💾 Save suppliers", type="primary"):
        for _, r in edited.iterrows():
            if pd.notna(r.get("id")):
                ex("""update suppliers set name=%s, phone=%s, alt_phone=%s, rep_name=%s,
                         email=%s, mpesa_paybill=%s, code=%s where id=%s""",
                   (r["name"], r["phone"], r["alt_phone"], r["rep_name"], r["email"],
                    r["mpesa_paybill"], r["code"], r["id"]))
            elif pd.notna(r.get("name")):
                ex("""insert into suppliers (pharmacy_id, code, name, phone, alt_phone,
                          rep_name, email, mpesa_paybill)
                      values (%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (PID, r.get("code"), r["name"], r.get("phone"), r.get("alt_phone"),
                    r.get("rep_name"), r.get("email"), r.get("mpesa_paybill")))
        st.success("Saved.")
        st.rerun()


# ============================================================ setup / onboarding
elif page == "Setup":
    from onboarding import setup_page
    setup_page(q, ex, PID, me)


# ============================================================ manual upload
elif page == "Manual upload":
    st.header("Manual upload")
    st.caption("The fallback for every automated path. Nothing here is a happy path — "
               "each option exists because the automated version can be unavailable "
               "on the day, and 'we could not receive stock today' is not an "
               "acceptable answer.")

    tab_inv, tab_pos = st.tabs(["Supplier invoice", "phAMACore export"])

    with tab_inv:
        st.markdown("**Supplier invoice → Loop A**")
        st.caption("Use when WhatsApp is down, the invoice is a PDF emailed by the "
                   "distributor, or you are receiving from a desk rather than the "
                   "counter. Identical pipeline to the photo path.")
        who = st.selectbox("Receiving on behalf of",
                           list({f"{s['name']} · {s['role']}": s for s in staff}),
                           key="mu_staff") if staff else None
        files = st.file_uploader("Invoice pages", type=["jpg", "jpeg", "png", "pdf"],
                                 accept_multiple_files=True, key="mu_inv")
        if files and st.button("Extract and review", type="primary"):
            actor = {f"{s['name']} · {s['role']}": s for s in staff}[who]
            _api()
            with st.spinner(f"Reading {len(files)} page(s)…"):
                try:
                    from datetime import datetime as _dt
                    from db import upload as _upload
                    import grn as _grn
                    paths = []
                    for i, f in enumerate(files, 1):
                        ext = (f.name or "p.jpg").rsplit(".", 1)[-1].lower()
                        p = (f"{_dt.utcnow():%Y/%m}/manual/"
                             f"{uuid.uuid4().hex[:10]}_{i}.{ext}")
                        _upload(BUCKET_INV, p, f.getvalue(),
                                "application/pdf" if ext == "pdf" else "image/jpeg")
                        paths.append(p)
                    grn_id = _grn.persist_from_paths(paths, actor)
                    if grn_id:
                        st.success("Extracted. Open **Receiving** to review and approve.")
                        st.code(_grn.render_summary(grn_id), language=None)
                    else:
                        st.error("Could not read an invoice from those pages.")
                except Exception as e:
                    st.error(f"Failed: {e}")

    with tab_pos:
        st.markdown("**phAMACore export → Loop B**")
        st.caption("Use when the PC agent is not installed yet, is offline, or the "
                   "pharmacy PC has no internet. Same three shapes the agent "
                   "recognises: transaction-level sales, monthly totals, or a stock "
                   "snapshot. Detected automatically from the headers.")
        up = st.file_uploader("CSV / XLSX from phAMACore",
                              type=["csv", "xlsx", "xls", "txt"], key="mu_pos")
        if up and st.button("Import", type="primary", key="mu_pos_btn"):
            _api()
            with st.spinner("Classifying and importing…"):
                try:
                    import sys as _sys, tempfile, os as _os
                    agent_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                              "..", "agent")
                    if agent_dir not in _sys.path:
                        _sys.path.insert(0, agent_dir)
                    from pathlib import Path as _P
                    from agent import classify_and_parse
                    with tempfile.NamedTemporaryFile(
                            delete=False, suffix=_os.path.splitext(up.name)[1]) as tf:
                        tf.write(up.getvalue())
                        tmp = tf.name
                    kind, rows = classify_and_parse(_P(tmp))
                    _os.unlink(tmp)
                    if not rows:
                        st.error(f"Detected shape: **{kind}** — no usable rows. The "
                                 f"headers did not match any known phAMACore export.")
                    else:
                        st.info(f"Detected **{kind}** · {len(rows)} rows")
                        import manual_ingest
                        res = manual_ingest.ingest(kind, rows, PID)
                        st.success(res)
                        st.dataframe(pd.DataFrame(rows[:20]),
                                     use_container_width=True, hide_index=True)
                except Exception as e:
                    st.error(f"Failed: {e}")


# ============================================================ system
else:
    st.header("System health")
    c1, c2, c3 = st.columns(3)
    c1.metric("Products", q("select count(*) n from products where pharmacy_id=%s",
                            (PID,))[0]["n"])
    c2.metric("Batches in stock",
              q("select count(*) n from batches where pharmacy_id=%s and qty_pieces>0",
                (PID,))[0]["n"])
    c3.metric("Customers", q("select count(*) n from customers where pharmacy_id=%s",
                             (PID,))[0]["n"])

    st.subheader("Ledger integrity")
    drift = q("""select b.id, p.name, b.qty_pieces,
                        coalesce(sum(m.delta_pieces),0) as ledger
                   from batches b
                   join products p on p.id=b.product_id
                   left join stock_movements m on m.batch_id=b.id
                  where b.pharmacy_id=%s
                  group by b.id, p.name, b.qty_pieces
                 having b.qty_pieces <> coalesce(sum(m.delta_pieces),0)""", (PID,))
    if drift:
        st.error(f"{len(drift)} batch(es) disagree with the movement ledger. "
                 "Do not trust stock figures until this is fixed.")
        st.dataframe(pd.DataFrame(drift), use_container_width=True, hide_index=True)
    else:
        st.success("Every batch matches its movement ledger.")

    st.subheader("Recent cron runs")
    st.dataframe(pd.DataFrame(q(
        """select job, status, detail, started_at, ended_at from job_runs
            where pharmacy_id = %s or pharmacy_id is null
            order by started_at desc limit 30""", (PID,))),
        use_container_width=True, hide_index=True)

    st.subheader("Unhandled messages")
    st.caption("Phone and error only. Message bodies are patient-identifiable under "
               "the Data Protection Act 2019 and are deliberately not rendered here.")
    st.dataframe(pd.DataFrame(q(
        """select from_phone, msg_type, error, created_at from wa_messages
            where pharmacy_id = %s and direction='in'
              and (handled=false or error is not null)
            order by created_at desc limit 30""", (PID,))),
        use_container_width=True, hide_index=True)

    st.subheader("Approval PINs")
    st.caption("A pharmacist needs a PIN to approve prescriptions over WhatsApp. "
               "Without one they cannot approve at all. 4 digits, not a birthday.")
    pin_staff = q("""select id, name, role, approval_pin, pin_locked_until
                       from staff where pharmacy_id=%s and is_active
                        and role in ('pharmacist','owner','manager')
                      order by name""", (PID,))
    for ps in pin_staff:
        c1, c2, c3 = st.columns([3, 2, 1])
        locked = bool(ps["pin_locked_until"]) and ps["pin_locked_until"] > datetime.now(
            ps["pin_locked_until"].tzinfo)
        c1.write(f"**{ps['name']}** · {ps['role']}" + ("  🔒 locked" if locked else ""))
        newpin = c2.text_input("New PIN", key=f"pin{ps['id']}", max_chars=6,
                               type="password",
                               placeholder="set" if ps["approval_pin"] else "not set")
        if c3.button("Save", key=f"sp{ps['id']}"):
            if not newpin.isdigit() or len(newpin) < 4:
                st.error("PIN must be at least 4 digits.")
            else:
                ex("""update staff set approval_pin=%s, pin_failed_count=0,
                          pin_locked_until=null where id=%s""", (newpin, ps["id"]))
                st.success(f"PIN set for {ps['name']}.")
                st.rerun()

    st.subheader("Duty roster")
    st.caption("Who is on duty each weekday. Prescriptions and the morning briefing go "
               "to the person on shift, not to five phones at once.")
    _WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]
    roster = q("""select r.weekday, s.name, s.id as staff_id
                    from duty_roster r join staff s on s.id = r.staff_id
                   where r.pharmacy_id=%s and r.on_date is null
                   order by r.weekday""", (PID,))
    by_day = {}
    for row in roster:
        by_day.setdefault(row["weekday"], []).append(row["name"])
    staff_pick = {s["name"]: s["id"] for s in staff}
    rc1, rc2, rc3 = st.columns([2, 2, 1])
    day_label = rc1.selectbox("Weekday", _WEEKDAYS, key="rota_day")
    who = rc2.selectbox("On duty", list(staff_pick), key="rota_who")
    if rc3.button("Add", key="rota_add"):
        ex("""insert into duty_roster (pharmacy_id, staff_id, weekday)
              values (%s,%s,%s)
              on conflict do nothing""",
           (PID, staff_pick[who], _WEEKDAYS.index(day_label)))
        st.success(f"{who} added to {day_label}.")
        st.rerun()
    st.dataframe(pd.DataFrame([{"Weekday": _WEEKDAYS[d],
                                "On duty": ", ".join(names)}
                               for d, names in sorted(by_day.items())])
                 if by_day else pd.DataFrame([{"Weekday": "—", "On duty": "nobody set"}]),
                 use_container_width=True, hide_index=True)

    st.subheader("Pharmacy PC agent")
    ag = q("""select machine_name, agent_version, ingest_mode, db_engine,
                     last_seen_at, suspended from agents where pharmacy_id=%s""", (PID,))
    if not ag:
        st.warning("No agent enrolled. Stock reflects only what Pharma OS received and "
                   "sold — not the till. Create an enrolment token below.")
        if st.button("Generate enrolment token"):
            import secrets as _s
            tok = _s.token_urlsafe(12)
            ex("""insert into agents (pharmacy_id, enrolment_token, ingest_mode)
                  values (%s,%s,'unknown')""", (PID, tok))
            st.code(tok, language=None)
            st.caption("Paste this into config.ini on the pharmacy PC. One-time use.")
    else:
        st.dataframe(pd.DataFrame(ag), use_container_width=True, hide_index=True)

    st.subheader("Unmatched payments")
    st.caption("Money received that we could not tie to an order. Never leave these.")
    st.dataframe(pd.DataFrame(q("""select amount, phone, mpesa_receipt, created_at
                                    from payments where pharmacy_id=%s
                                     and order_id is null and status='success'
                                    order by created_at desc limit 20""", (PID,))),
                 use_container_width=True, hide_index=True)

    st.subheader("Staff (WhatsApp access)")
    sdf = st.data_editor(pd.DataFrame(q(
        """select id, phone, name, role, ppb_reg_no, is_active from staff
            where pharmacy_id=%s order by name""", (PID,))),
        use_container_width=True, hide_index=True, num_rows="dynamic", disabled=["id"])
    if st.button("💾 Save staff"):
        for _, r in sdf.iterrows():
            if pd.notna(r.get("id")):
                ex("""update staff set phone=%s, name=%s, role=%s, ppb_reg_no=%s,
                         is_active=%s where id=%s""",
                   (r["phone"], r["name"], r["role"], r["ppb_reg_no"],
                    bool(r["is_active"]), r["id"]))
            elif pd.notna(r.get("phone")):
                ex("""insert into staff (pharmacy_id, phone, name, role, ppb_reg_no)
                      values (%s,%s,%s,%s,%s) on conflict (phone) do nothing""",
                   (PID, r["phone"], r["name"], r["role"], r.get("ppb_reg_no")))
        st.success("Saved.")
        st.rerun()
