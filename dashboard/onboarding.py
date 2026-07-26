"""Pharmacy onboarding: create a pharmacy, then add the phone numbers.

WHY PHONE NUMBERS AND NOT PASSWORDS

There is deliberately no per-user password here. In this system a person's identity
IS their phone number, because that is the only credential that matters to the surface
that actually gets used:

  * WhatsApp answers a number or it does not. `staff.phone` is the whitelist. A
    number that is not in this table gets no reply, cannot approve anything, and
    cannot see stock. Adding a number IS granting access.
  * `approval_pin` is a second factor for the one action that carries legal weight
    (a pharmacist releasing a prescription). It is not a login.
  * The dashboard sits behind one shared DASHBOARD_PASSWORD.

That is a real, stated trade-off, not an oversight, and it is fine for a single-site
pilot where everyone with dashboard access is in the same room. It stops being fine
the moment two pharmacies share one deployment, because the tenant then comes from a
dropdown rather than from an authenticated session — anyone who can reach the
dashboard can switch pharmacy. See ARCHITECTURE_V2 §"auth" for the upgrade path:
per-user credentials, sessions, and RLS so the DATABASE enforces separation instead
of every query remembering to.

Do not ship this to a second paying customer without that work.
"""
import re

import streamlit as st

ROLES = ["owner", "manager", "pharmacist", "attendant"]

ROLE_HELP = {
    "owner": "Gets the morning digest, expiry alerts, VARIANCE, and approves purchase "
             "orders with a PIN.",
    "manager": "Same operational commands as the owner. Can approve POs.",
    "pharmacist": "Can verify prescriptions over WhatsApp with a PIN. Needs a PPB "
                  "registration number — it goes on the record for every approval.",
    "attendant": "Can photograph invoices and receive stock. Cannot approve "
                 "prescriptions or purchase orders.",
}


def norm_phone(raw: str) -> str:
    """Kenyan MSISDN -> 2547XXXXXXXX. Mirrors api/utils.norm_phone.

    Kept consistent on purpose: if the dashboard stores '0713755274' and WhatsApp
    reports '254713755274', the number is silently not whitelisted and the staff
    member's messages are ignored with no error anywhere.
    """
    d = re.sub(r"\D", "", str(raw or ""))
    if not d:
        return ""
    if d.startswith("254"):
        return d
    if d.startswith("0"):
        return "254" + d[1:]
    if len(d) == 9:
        return "254" + d
    return d


def _valid(phone: str) -> bool:
    return bool(re.fullmatch(r"254(7|1)\d{8}", phone))


def first_run(q, ex) -> None:
    """Shown only when `pharmacies` is empty — the database has to be enterable."""
    st.title("💊 Set up your pharmacy")
    st.caption("Nothing exists yet. Create the pharmacy, then add the owner's "
               "WhatsApp number.")

    with st.form("first_pharmacy"):
        name = st.text_input("Pharmacy name *", placeholder="Pharma Chemist, Kikuyu")
        c1, c2 = st.columns(2)
        paybill = c1.text_input("M-Pesa Paybill", placeholder="4166919")
        licence = c2.text_input("PPB licence no.")
        wa_number = st.text_input(
            "The pharmacy's WhatsApp line",
            placeholder="0712 345 678",
            help="The SIM you pair with GOWA. Use a dedicated line, not a personal "
                 "number — pairing drives a WhatsApp Web session and the number "
                 "carries the ban risk.")
        owner_name = st.text_input("Owner's name *", placeholder="Ryan")
        owner_phone = st.text_input("Owner's WhatsApp number *",
                                    placeholder="0713 755 274")
        go = st.form_submit_button("Create pharmacy", type="primary")

    if not go:
        return

    op = norm_phone(owner_phone)
    if not name.strip() or not owner_name.strip() or not op:
        st.error("Pharmacy name, owner name and owner number are all required.")
        return
    if not _valid(op):
        st.error(f"'{owner_phone}' does not look like a Kenyan mobile number "
                 f"(got {op}).")
        return
    if q("select 1 from staff where phone=%s", (op,)):
        # staff.phone is globally UNIQUE, not unique-per-pharmacy.
        st.error(f"{op} is already registered to a pharmacy.")
        return

    row = q("""insert into pharmacies (name, mpesa_paybill, ppb_licence, wa_number)
               values (%s,%s,%s,%s) returning id""",
            (name.strip(), paybill.strip() or None, licence.strip() or None,
             norm_phone(wa_number) or None))
    pid = str(row[0]["id"])
    ex("""insert into staff (pharmacy_id, phone, name, role, is_active)
          values (%s,%s,%s,'owner',true)""", (pid, op, owner_name.strip()))

    st.success(f"Created **{name}**. Owner {owner_name} ({op}) can now message the "
               f"pharmacy's WhatsApp line.")
    st.info(f"Add this to your `.env` so the API and jobs target this pharmacy:\n\n"
            f"`PHARMACY_ID={pid}`")
    st.session_state["pid"] = pid
    st.button("Continue", type="primary", on_click=st.rerun)


def setup_page(q, ex, PID: str, me: dict) -> None:
    """The Setup page: pharmacy details, staff numbers, PINs, new pharmacies."""
    st.header("Setup")

    ph = q("""select name, ppb_licence, mpesa_paybill, wa_number
                from pharmacies where id=%s""", (PID,))[0]

    # ---------------------------------------------------------------- pharmacy
    st.subheader("Pharmacy")
    with st.form("edit_pharmacy"):
        name = st.text_input("Name", value=ph["name"] or "")
        c1, c2 = st.columns(2)
        paybill = c1.text_input("M-Pesa Paybill", value=ph["mpesa_paybill"] or "")
        licence = c2.text_input("PPB licence no.", value=ph["ppb_licence"] or "")
        wa_number = st.text_input(
            "Pharmacy WhatsApp line", value=ph["wa_number"] or "",
            help="The SIM paired with GOWA. Customers and staff message THIS number.")
        if st.form_submit_button("Save"):
            ex("""update pharmacies set name=%s, mpesa_paybill=%s, ppb_licence=%s,
                      wa_number=%s where id=%s""",
               (name.strip(), paybill.strip() or None, licence.strip() or None,
                norm_phone(wa_number) or None, PID))
            st.success("Saved.")
            st.rerun()

    st.caption(f"`PHARMACY_ID={PID}` — the API and cron jobs read this from `.env`.")
    st.divider()

    # ---------------------------------------------------------------- staff
    st.subheader("Who the system talks to")
    st.caption("WhatsApp answers these numbers and no others. Adding a number here "
               "grants access; deactivating it revokes access immediately. There is "
               "no separate password — the number *is* the credential.")

    rows = q("""select id, name, role, phone, ppb_reg_no, is_active,
                       approval_pin is not null as has_pin, pin_locked_until
                  from staff where pharmacy_id=%s
                 order by case role when 'owner' then 0 when 'manager' then 1
                                    when 'pharmacist' then 2 else 3 end, name""",
             (PID,))

    for s in rows:
        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 2, 2])
            badge = "" if s["is_active"] else "  ⏸️ inactive"
            pin = "🔑 PIN set" if s["has_pin"] else "⚠️ no PIN"
            if s["role"] in ("owner", "manager", "pharmacist"):
                c1.markdown(f"**{s['name']}** · {s['role']}{badge}  \n"
                            f"`{s['phone']}` · {pin}")
            else:
                c1.markdown(f"**{s['name']}** · {s['role']}{badge}  \n`{s['phone']}`")
            if s["role"] == "pharmacist" and not s["ppb_reg_no"]:
                c1.warning("No PPB number — required on every prescription approval.")

            # A pharmacist with no PIN cannot approve anything at all, so make the
            # fix available right here rather than on a different page.
            if s["role"] in ("owner", "manager", "pharmacist"):
                newpin = c2.text_input("Set PIN", key=f"pin{s['id']}", max_chars=6,
                                       type="password",
                                       placeholder="4-6 digits")
                if c2.button("Save PIN", key=f"sp{s['id']}"):
                    if not newpin.isdigit() or not (4 <= len(newpin) <= 6):
                        st.error("PIN must be 4-6 digits.")
                    else:
                        ex("""update staff set approval_pin=%s, pin_failed_count=0,
                                  pin_locked_until=null where id=%s""",
                           (newpin, s["id"]))
                        st.success(f"PIN set for {s['name']}.")
                        st.rerun()

            if s["is_active"]:
                if c3.button("Deactivate", key=f"da{s['id']}"):
                    # Never delete: stock movements, GRNs and prescription approvals
                    # reference staff.id, and the audit trail must survive the person
                    # leaving.
                    ex("update staff set is_active=false where id=%s", (s["id"],))
                    st.rerun()
            else:
                if c3.button("Reactivate", key=f"ra{s['id']}"):
                    ex("update staff set is_active=true where id=%s", (s["id"],))
                    st.rerun()

    st.markdown("**Add someone**")
    with st.form("add_staff", clear_on_submit=True):
        c1, c2 = st.columns(2)
        nm = c1.text_input("Name *")
        phone = c2.text_input("WhatsApp number *", placeholder="0713 755 274")
        c3, c4 = st.columns(2)
        role = c3.selectbox("Role *", ROLES)
        ppb = c4.text_input("PPB reg no.", help="Required for pharmacists.")
        st.caption(ROLE_HELP[role])
        if st.form_submit_button("Add", type="primary"):
            p = norm_phone(phone)
            if not nm.strip() or not p:
                st.error("Name and number are required.")
            elif not _valid(p):
                st.error(f"'{phone}' does not look like a Kenyan mobile number "
                         f"(got {p}).")
            elif role == "pharmacist" and not ppb.strip():
                # PPB attribution is the legal core of the product; a pharmacist
                # without a registration number cannot lawfully sign an approval.
                st.error("A pharmacist needs a PPB registration number.")
            elif q("select 1 from staff where phone=%s", (p,)):
                st.error(f"{p} is already registered.")
            else:
                ex("""insert into staff (pharmacy_id, phone, name, role, ppb_reg_no,
                            is_active) values (%s,%s,%s,%s,%s,true)""",
                   (PID, p, nm.strip(), role, ppb.strip() or None))
                st.success(f"{nm} added. They can message the pharmacy line now.")
                st.rerun()

    st.divider()

    # ---------------------------------------------------------------- new pharmacy
    with st.expander("Add another pharmacy"):
        st.caption("⚠️ One deployment currently serves whichever pharmacy is selected "
                   "in the sidebar, and that selection is not tied to a login — "
                   "anyone with the dashboard password can switch between them. Fine "
                   "while every pharmacy is yours; not fine for a second paying "
                   "customer. Per-user auth and RLS come first.")
        with st.form("new_pharmacy", clear_on_submit=True):
            n = st.text_input("Pharmacy name *")
            c1, c2 = st.columns(2)
            onm = c1.text_input("Owner name *")
            oph = c2.text_input("Owner WhatsApp *")
            if st.form_submit_button("Create"):
                p = norm_phone(oph)
                if not n.strip() or not onm.strip() or not _valid(p):
                    st.error("All three fields are required, with a valid number.")
                elif q("select 1 from staff where phone=%s", (p,)):
                    st.error(f"{p} is already registered.")
                else:
                    r = q("insert into pharmacies (name) values (%s) returning id",
                          (n.strip(),))
                    npid = str(r[0]["id"])
                    ex("""insert into staff (pharmacy_id, phone, name, role, is_active)
                          values (%s,%s,%s,'owner',true)""", (npid, p, onm.strip()))
                    st.success(f"Created {n}. Switch to it in the sidebar.")
                    st.rerun()
