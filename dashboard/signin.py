"""The sign-in page.

ON THE OTHER SIGN-IN OPTIONS

The architecture review is right that the end state is Supabase Auth with Google OAuth
and email magic links, identity living in `auth.users`, and WhatsApp demoted to a
communication channel rather than a credential. That is the correct destination and
this page is laid out for it: the provider buttons are already here, disabled, so the
shape of the final page is visible and adding them later is wiring rather than
redesign.

They are NOT enabled yet, deliberately. Turning them on is not a UI change — it means
`staff.supabase_user_id`, an invitations table, RLS policies keyed on `auth.uid()`,
and dropping `service_role` for user-facing queries. Shipping the buttons before that
work exists would give the *appearance* of per-user identity while every session still
shares one password and one tenant, which is worse than an honest shared password:
people would reasonably assume actions are attributable to them when they are not.

So: one shared password today, stated plainly on the page, with the upgrade path
visible. The PIN on `staff` is what carries per-person attribution for the actions
that legally need it (prescription release, PO approval), and that is unaffected.
"""
import base64
import os
from pathlib import Path

import streamlit as st


def _b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def sign_in_page(icon_path: str, orange: str, app_password: str, hmac) -> None:
    logo = _b64(icon_path)

    st.markdown(f"""
    <style>
      #MainMenu, footer, header {{visibility: hidden;}}
      .block-container {{padding-top: 4rem; max-width: 430px;}}
      .po-card {{
        text-align: center;
        padding: 8px 0 4px;
      }}
      .po-logo {{ width: 76px; height: 76px; margin-bottom: 14px; }}
      .po-name {{
        font-size: 30px; font-weight: 700; letter-spacing: -0.5px;
        color: var(--text-color, #17252a); margin: 0;
      }}
      .po-sub {{
        font-size: 14px; color: #6e7a80; margin: 6px 0 26px;
      }}
      .po-or {{
        display: flex; align-items: center; gap: 12px;
        color: #9aa5ab; font-size: 12px; margin: 20px 0 14px;
      }}
      .po-or:before, .po-or:after {{
        content: ""; flex: 1; height: 1px; background: #e3e8ea;
      }}
      .po-soon button {{
        width: 100%;
        border: 1px solid #e3e8ea !important;
        background: transparent !important;
        color: #9aa5ab !important;
      }}
      .po-foot {{
        text-align: center; font-size: 11px; color: #9aa5ab; margin-top: 30px;
      }}
      div[data-testid="stForm"] {{ border: none; padding: 0; }}
    </style>

    <div class="po-card">
      <img class="po-logo" src="data:image/png;base64,{logo}"/>
      <p class="po-name">Pharma OS</p>
      <p class="po-sub">Sign in to your pharmacy workspace</p>
    </div>
    """, unsafe_allow_html=True)

    from identity import AUTH_MODE, send_code, verify_code

    # Which sign-in methods are offered is driven by AUTH_MODE, which defaults to the
    # old shared-password behaviour. An auth migration must never be able to lock a
    # pharmacy out of its own stock system mid-shift.
    personal = AUTH_MODE in ("whatsapp", "strict")
    shared_ok = AUTH_MODE in ("shared", "whatsapp")

    if personal:
        _whatsapp_sign_in(send_code, verify_code)

    if personal and shared_ok:
        st.markdown('<div class="po-or">OR</div>', unsafe_allow_html=True)

    if shared_ok:
        with st.form("signin", clear_on_submit=False):
            pw = st.text_input(
                "Password", type="password",
                placeholder="Pharmacy password" if not personal
                            else "Shared pharmacy password",
                label_visibility="collapsed")
            go = st.form_submit_button(
                "Sign in" if not personal else "Sign in with the shared password",
                type="primary" if not personal else "secondary",
                use_container_width=True)

        if go:
            # hmac.compare_digest, not ==, so a wrong password cannot be recovered a
            # character at a time from response timing.
            if hmac.compare_digest(pw, app_password):
                st.session_state["authed"] = True
                st.session_state["auth_method"] = "shared_password"
                _audit_shared(True)
                st.rerun()
            else:
                # Never echo the expected value, or any hint about it, to someone who
                # has not authenticated.
                _audit_shared(False)
                st.error("Wrong password.")

    if not personal:
        st.markdown('<div class="po-or">OR</div>', unsafe_allow_html=True)
        st.markdown('<div class="po-soon">', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        c1.button("Sign in with WhatsApp", disabled=True, use_container_width=True,
                  help="Ready. Set AUTH_MODE=whatsapp in .env to switch on per-user "
                       "sign-in; the shared password keeps working alongside it.")
        c2.button("Continue with Google", disabled=True, use_container_width=True,
                  help="Needs a Google Cloud project and OAuth consent screen "
                       "configured in Supabase first.")
        st.markdown("</div>", unsafe_allow_html=True)

    foot = ("Signed-in actions are recorded against you. Prescription and "
            "purchase-order approvals still require your PIN."
            if personal else
            "One shared password for now. Prescription and purchase-order approvals "
            "are attributed individually by staff PIN.")
    st.markdown(f'<p class="po-foot">{foot}<br/>Pharma OS</p>',
                unsafe_allow_html=True)


def _audit_shared(ok: bool) -> None:
    try:
        from db_helpers import ex_
        from identity import log_shared_password_login
        log_shared_password_login(ex_, os.getenv("PHARMACY_ID"), ok)
    except Exception:
        pass


def _whatsapp_sign_in(send_code, verify_code) -> None:
    """Two steps in one place: request a code, then enter it."""
    from db_helpers import ex_, q_

    stage = st.session_state.get("signin_stage", "phone")

    if stage == "phone":
        with st.form("wa_request", clear_on_submit=False):
            phone = st.text_input("WhatsApp number", placeholder="0713 755 274",
                                  label_visibility="collapsed")
            sent = st.form_submit_button("Send me a code on WhatsApp",
                                         type="primary", use_container_width=True)
        if sent:
            ok, msg = send_code(phone, q_, ex_)
            if ok:
                st.session_state["signin_stage"] = "code"
                st.session_state["signin_phone"] = phone
                st.info(msg)
                st.rerun()
            else:
                st.error(msg)
        return

    phone = st.session_state.get("signin_phone", "")
    st.caption(f"Code sent to {phone} on WhatsApp. It expires in 10 minutes.")
    with st.form("wa_verify", clear_on_submit=False):
        code = st.text_input("6-digit code", max_chars=6, placeholder="123456",
                             label_visibility="collapsed")
        ok_btn = st.form_submit_button("Sign in", type="primary",
                                       use_container_width=True)
    c1, c2 = st.columns(2)
    if c1.button("Send a new code", use_container_width=True):
        send_code(phone, q_, ex_)
        st.info("A new code is on its way.")
    if c2.button("Use a different number", use_container_width=True):
        st.session_state.pop("signin_stage", None)
        st.rerun()

    if ok_btn:
        staff, msg = verify_code(phone, code, q_, ex_)
        if staff:
            st.session_state["authed"] = True
            st.session_state["auth_method"] = "whatsapp"
            st.session_state["staff_id"] = str(staff["id"])
            st.session_state["pid"] = str(staff["pharmacy_id"])
            st.session_state.pop("signin_stage", None)
            st.rerun()
        else:
            st.error(msg)
