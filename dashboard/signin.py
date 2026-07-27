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

    # The working path. In a form so Enter submits — staff type a password and hit
    # return; making them reach for the mouse is a small daily irritation.
    with st.form("signin", clear_on_submit=False):
        pw = st.text_input("Password", type="password",
                           placeholder="Pharmacy password",
                           label_visibility="collapsed")
        go = st.form_submit_button("Sign in", type="primary",
                                   use_container_width=True)

    if go:
        # hmac.compare_digest, not ==, so a wrong password cannot be recovered a
        # character at a time from response timing.
        if hmac.compare_digest(pw, app_password):
            st.session_state["authed"] = True
            st.rerun()
        else:
            # Never echo the expected value, or any hint about it, to someone who has
            # not authenticated.
            st.error("Wrong password.")

    st.markdown('<div class="po-or">OR</div>', unsafe_allow_html=True)

    st.markdown('<div class="po-soon">', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    c1.button("Continue with Google", disabled=True, use_container_width=True,
              help="Coming with per-user accounts. Needs Supabase Auth, "
                   "staff.supabase_user_id and RLS policies first — the button "
                   "without those would imply attribution the system cannot yet honour.")
    c2.button("Email magic link", disabled=True, use_container_width=True,
              help="Coming with per-user accounts, alongside Google sign-in.")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown(
        '<p class="po-foot">One shared password for now. Prescription and purchase-order '
        'approvals are attributed individually by staff PIN.<br/>Pharma OS</p>',
        unsafe_allow_html=True)
