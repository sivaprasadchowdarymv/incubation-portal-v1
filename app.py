"""Phase 1 Incubation RAG Agent - Streamlit UI. Run: streamlit run app.py"""
from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

import cloud
from evaluation.metrics import workflow_summary
from notifications.outbox import count_waiting, deliver_pending, describe, send_test_email, smtp_configured
from notifications.service import outbox_counts, recent_failures, send_deadline_reminders, send_review_reminders
from persist import snapshot as persist
from rag import providers
from rag.answer import answer
from rag.index import HybridIndex
from rag.ollama import make_client
from security.auth import AuthError
from workflow import evaluation as ev
from workflow import queries as qs
from workflow import services as sv
from workflow import setup, stats
from workflow.context import open_app

st.set_page_config(page_title="Incubation Portal", page_icon="🌱", layout="wide")
st.markdown("""<style>
[data-testid="stMetric"]{background:var(--secondary-background-color);border-radius:12px;padding:12px 16px}
.hero{background:linear-gradient(90deg,#0b5cab 0%,#1c8a5a 100%);color:#fff;border-radius:14px;padding:14px 22px;margin-bottom:12px}
.hero h2{color:#fff;margin:0;font-size:1.45rem}.hero p{margin:2px 0 0;opacity:.9}
.chip{display:inline-block;background:rgba(255,255,255,.18);border-radius:999px;padding:2px 12px;margin:6px 6px 0 0;font-size:.85rem}
.step{border-radius:10px;padding:8px 10px;text-align:center;font-size:.85rem;border:1px solid rgba(128,128,128,.25)}
.step.done{background:#e6f4ea;border-color:#34a853}.step.now{background:#fff4e5;border-color:#f9ab00}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def app_ctx():
    cloud.boot(st)                       # cloud hosting only: secrets -> settings, restore saved data from Hugging Face
    ctx = open_app()
    cloud.ready(*ctx, make_client(ctx[1]))
    if cloud.enabled() or os.environ.get("RUN_WORKER_IN_APP") == "1":
        cloud.background()               # emails, reminders and data upload without a second window
    return ctx


conn, settings, base_policy = app_ctx()
policy = setup.effective_policy(conn, base_policy)  # policy.json + admin-uploaded settings
client = make_client(settings, conn)


@st.cache_data(ttl=30)
def model_status():
    return client.status()


def load_index():
    if HybridIndex.needs_rebuild(settings.kb_dir, settings.index_dir):
        return None
    key = (settings.index_dir / "meta.json").stat().st_mtime
    if st.session_state.get("_idx_key") != key:
        st.session_state["_idx"], st.session_state["_idx_key"] = HybridIndex.load(settings.index_dir), key
    return st.session_state["_idx"]


_LAST_OK: list[str] = []   # confirmation of the last successful action in this script run


def refresh():
    """Rerun the page, carrying the last confirmation over so the user still sees it."""
    if _LAST_OK:
        st.session_state["_flash"] = _LAST_OK[-1]
    st.rerun()


def run(fn, *a, ok: str | None = None, **kw):
    """Call a backend service and surface its real outcome."""
    try:
        res = fn(*a, **kw)
        if ok:
            st.success(ok)
            _LAST_OK.append(ok)
        persist.backup_soon()  # copy the change to persistent storage (no-op on PC installs)
        if smtp_configured(settings) and policy.get("notifications", {}).get("auto_send", True):
            try:  # send acknowledgements/alerts right away; failures stay queued for the worker to retry
                deliver_pending(conn, settings, limit=50)
            except Exception:  # never let email problems break the workflow action
                pass
        return True if res is None else res  # success must be truthy so callers refresh the page
    except sv.SubmissionBlocked as e:
        st.error(f"Blocked ({e.code}): {e}. No attempt was consumed.")
    except (sv.WorkflowError, AuthError, ValueError) as e:
        st.error(str(e))
    return None


SEV_ICON = {"error": "🔴", "warning": "🟠", "info": "🟢"}


def show_report(rep: dict):
    c = st.columns(4)
    c[0].metric("Status", rep["status"].replace("_", " "))
    c[1].metric("Attempt", f"{rep['attempt_no']} / {policy['submission']['max_attempts']}")
    c[2].metric("Words", rep["word_count"] if rep["word_count"] is not None else "-")
    c[3].metric("Human review", rep["review_status"])
    for sev in ("error", "warning", "info"):
        items = [x for x in rep["checks"] if x["severity"] == sev]
        if items:
            st.markdown(f"**{SEV_ICON[sev]} {sev.title()}**")
            for x in items:
                st.write(f"`{x['check_id']}` **{x['result']}** — {x['message']}" + (f"  \n↳ _{x['guidance']}_" if x["guidance"] and x["result"] != "pass" else ""))
                if x["evidence"]:
                    st.caption(f"evidence: {x['evidence']}")
    if rep["similarity"]:
        st.markdown("**Similarity findings** (indicators only, never automatic rejection)")
        st.dataframe(pd.DataFrame(rep["similarity"]), hide_index=True, width="stretch")
    if rep["decisions"]:
        st.markdown("**Review decisions**")
        st.dataframe(pd.DataFrame(rep["decisions"]), hide_index=True, width="stretch")
    with st.expander("Status history"):
        st.dataframe(pd.DataFrame(rep["history"]), hide_index=True, width="stretch")
    st.caption(f"Submission #{rep['submission_id']} · team {rep['team_code']} · {rep['file_name']} · submitted {rep['submitted_at']} · rules v{rep['policy_version']}")
    b = st.columns(2)
    try:
        fname, fdata = sv.submission_file(conn, user, rep["submission_id"], settings.storage_dir)
        b[0].download_button(f"⬇ Open submitted file ({fname})", fdata, fname, key=f"file{rep['submission_id']}",
                             on_click=sv.log_file_download, args=(conn, user, rep["submission_id"], fname))
    except Exception as exc:
        b[0].caption(f"Submitted file unavailable: {exc}")
    b[1].download_button("Download report (JSON)", json.dumps(rep, indent=2), f"report_{rep['submission_id']}.json", "application/json", key=f"dl{rep['submission_id']}")


# ---------------- auth ----------------
user = st.session_state.get("user")
with st.sidebar:
    st.title("🌱 Incubation Phase 1")
    if user:
        user = sv.refresh_user(conn, user)
        st.session_state["user"] = user
        st.write(f"Signed in as **{user.username}** ({user.role})")
        if st.button("Sign out"):
            st.session_state.clear()
            refresh()
    if user and user.role == "admin":  # AI status matters only to staff
        ms = model_status()
        st.caption((f"🟢 AI: {client.chat_model} (cloud)" if ms.get("provider") == "api" and ms["chat_ok"] else "🟢 Ollama (local AI)" if ms["reachable"]
                    else "⚪ AI off — add a key in Programme Setup → AI keys (everything else works)"))

if not user:
    st.header("Sign in")
    t1, t2 = st.tabs(["Sign in", "Student sign-up"])
    with t1, st.form("login"):
        un, pw = st.text_input("Username"), st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            try:
                u = sv.authenticate(conn, un, pw)
            except sv.LoginLocked as e:
                u = None
                st.error(str(e))
            else:
                if u:
                    st.session_state["user"] = u
                    refresh()
                st.error("Invalid username or password.")
    with t2, st.form("signup"):
        un, pw = st.text_input("Choose username"), st.text_input("Choose password (min 8)", type="password")
        sem = st.text_input("Your email (optional, for a confirmation)")
        scode = st.text_input("Sign-up code (from the incubation office)", type="password") if settings.signup_code else None
        if st.form_submit_button("Create student account") and run(sv.signup_student, conn, un, pw, sem, policy, scode, settings.signup_code,
                                                                     ok="Account created. Sign in."):
            pass
    st.stop()

ICON = {"My Team": "👥", "Submit": "📤", "History": "🗂️", "Policy Assistant": "💬", "Review Queue": "🧑‍⚖️", "Submissions": "📄",
        "Dashboard": "📊", "Programme Setup": "⚙️", "My Reviews": "📝", "Peer Review": "🎯", "Results": "🏅",
        "Ask the Incubation Team": "📨", "Queries": "📬", "AI Assistant": "🤖", "Users": "🔑", "Knowledge Base": "📚", "Notifications": "✉️", "Audit Log": "🧾", "Policy": "📜"}


def left_until(dt) -> str:
    secs = int((dt - sv.now_utc()).total_seconds())
    if secs <= 0:
        return "closed"
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f"{d} d {h} h left" if d else f"{h} h {m} min left"


def hero():
    nc = policy.get("notifications", {})
    st.markdown(f"<div class='hero'><h2>🌱 {nc.get('programme_name', 'Incubation Programme')}</h2>"
                f"<p>Phase 1 · registration, abstract submission and review</p>"
                f"<span class='chip'>⏰ Deadline {sv.deadline(policy).strftime('%d %b %Y, %I:%M %p')} · {left_until(sv.deadline(policy))}</span>"
                f"<span class='chip'>👤 {user.username} · {user.role}</span></div>", unsafe_allow_html=True)


def ps_card(ps: dict, track_name: str = ""):
    """Readable problem-statement card: summary, challenges and impact as bullet lists."""
    with st.container(border=True):
        st.markdown(f"**{ps['id']} · {ps['title']}**" + (f"  \n<small>{track_name}</small>" if track_name else ""), unsafe_allow_html=True)
        for line in (ps.get("description") or "").split("\n"):
            label, _, rest = line.partition(": ")
            if label in ("Challenges", "Expected impact") and rest:
                st.markdown(f"*{label}*\n" + "\n".join(f"- {x.strip()}" for x in rest.split(";") if x.strip()))
            elif label in ("Category", "Mapping") and rest:
                st.caption(f"{label}: {rest}")
            elif line.strip():
                st.write(line)


STATUS_LABEL = {"ACCEPTED_FOR_EVALUATION": "✅ Accepted", "PENDING_REVIEW": "🧑‍⚖️ Under review", "REVIEW_CLEARED": "🧑‍⚖️ Under review",
                "VALIDATING": "⏳ Checking", "SUBMITTED": "⏳ Checking", "RESUBMISSION_ALLOWED": "✏️ Fix & resubmit", "DROPPED": "⛔ Closed",
                "DEADLINE_MISSED": "⛔ Closed", "REJECTED_BY_AUTHORIZED_REVIEW": "⛔ Not accepted", None: "Not submitted"}


def progress_tracker(team_id):
    """Student journey: account -> team -> problem statement(s) -> submission(s) -> result."""
    t = conn.execute("SELECT ps_locked FROM teams WHERE id=?", (team_id,)).fetchone() if team_id else None
    entries = sv.team_entries(conn, team_id) if team_id else []
    stats = [e["last_status"] for e in entries]
    all_acc = bool(entries) and all(x == "ACCEPTED_FOR_EVALUATION" for x in stats)
    res = ev.team_results(conn, policy, user) if user.role == "student" and team_id else []
    if res:
        result = {"shortlisted": "🏅 Shortlisted", "waitlisted": "⏳ Waitlisted", "not_shortlisted": "Not shortlisted"}[res[0]["outcome"]]
        result += f" (+{len(res) - 1})" if len(res) > 1 else ""
    elif all_acc:
        result = "✅ Accepted · being evaluated"
    else:
        result = STATUS_LABEL.get(stats[0], "Result") if len(stats) == 1 and stats[0] else "Result"
    n = len(entries)
    done = [True, bool(team_id), bool(t and t["ps_locked"]), any(stats), all_acc]
    labels = ["Account", "Team registered", "Problem statement locked" if n <= 1 else f"{n} problem statements locked",
              "Abstract submitted" if n <= 1 else f"{sum(1 for x in stats if x)} of {n} abstracts submitted", result]
    now = done.index(False) if False in done else None
    cols = st.columns(5)
    for i, (c, lab) in enumerate(zip(cols, labels)):
        cls = "done" if done[i] else ("now" if i == now else "")
        c.markdown(f"<div class='step {cls}'>{'✔' if done[i] else i + 1} · {lab}</div>", unsafe_allow_html=True)


def choose_ps(cat: list[dict], exclude: set, key: str):
    """Track -> problem statement picker with a readable card and a searchable browser."""
    open_cat = [dict(d, problem_statements=[p_ for p_ in d["problem_statements"] if p_["id"] not in exclude]) for d in cat]
    open_cat = [d for d in open_cat if d["problem_statements"]]
    if not open_cat:
        st.info("There are no other problem statements to choose.")
        return None, None
    d = st.selectbox("Track (domain)", open_cat, key=f"{key}_d",
                     format_func=lambda x: f"{x['id']} — {x['name']} ({len(x['problem_statements'])} problem statements)")
    ps = st.selectbox("Problem statement", d["problem_statements"], key=f"{key}_p", format_func=lambda x: f"{x['id']} — {x['title']}")
    ps_card(ps, d["name"])
    with st.expander(f"📚 Browse all {sum(len(x['problem_statements']) for x in open_cat)} problem statements"):
        qq = st.text_input("Search title or description", key=f"{key}_q").lower()
        for dd in open_cat:
            for p_ in dd["problem_statements"]:
                if not qq or qq in (p_["title"] + " " + (p_["description"] or "")).lower():
                    ps_card(p_, dd["name"])
    return d, ps


PAGES = {
    "student": ["My Team", "Submit", "History", "Results", "Ask the Incubation Team"],
    "reviewer": ["My Reviews", "Review Queue", "Ask the Incubation Team"],
    "admin": ["Dashboard", "Programme Setup", "Review Queue", "Submissions", "Peer Review", "Queries", "Users", "Knowledge Base",
              "Notifications", "Audit Log", "Policy", "AI Assistant"],
}
_open_q = qs.counts(conn).get("open", 0) if user.role == "admin" else 0
page = st.sidebar.radio("Navigate", PAGES[user.role],
                        format_func=lambda p: f"{ICON.get(p, '•')} {p}" + (f" ({_open_q} open)" if p == "Queries" and _open_q else ""))
hero()
if _flash := st.session_state.pop("_flash", None):
    st.success(f"✅ {_flash}")

# ---------------- student pages ----------------
if page == "My Team":
    st.header("My Team")
    progress_tracker(user.team_id)
    if not user.team_id:
        st.info(f"Register your team ({policy['team']['min_members']}-{policy['team']['max_members']} members). Membership is locked after registration.")
        n = st.number_input("Number of members", 1, 4, 2)
        with st.form("reg"):
            name, email = st.text_input("Team name"), st.text_input("Team contact email")
            members = []
            for i in range(int(n)):
                c = st.columns(3)
                members.append({"full_name": c[0].text_input(f"Member {i+1} name"), "member_ref": c[1].text_input(f"Member {i+1} ID / roll no."),
                                "email": c[2].text_input(f"Member {i+1} email (optional)")})
            if st.form_submit_button("Register and lock team") and run(sv.register_team, conn, policy, user, name, email, members, ok="Team registered and locked."):
                refresh()
    else:
        t = sv.get_team(conn, user, user.team_id)
        st.subheader(f"{t['name']}  ·  {t['team_code']}  🔒")
        st.dataframe(pd.DataFrame(t["members"]), hide_index=True)
        cat = sv.catalog(conn)
        if t["ps_locked"]:
            entries = sv.team_entries(conn, user.team_id)
            mx = sv.max_entries(policy)
            st.markdown(f"#### 🔒 Your problem statements ({len(entries)} of {mx} allowed)")
            st.dataframe(pd.DataFrame([{"#": e["entry_no"], "Track": e["track"], "Problem statement": f"{e['ps_id']} — {e['ps_title']}",
                                        "Attempts used": f"{e['attempts_used']} / {policy['submission']['max_attempts']}",
                                        "Status": STATUS_LABEL.get(e["last_status"], e["last_status"])} for e in entries]),
                         hide_index=True, width="stretch")
            for e in entries:
                ps = conn.execute("SELECT * FROM problem_statements WHERE id=?", (e["ps_id"],)).fetchone()
                with st.expander(f"{e['entry_no']}. {e['ps_id']} — {e['ps_title']}"):
                    ps_card(dict(ps), e["track"])
            if len(entries) < mx and sv.before_deadline(policy, sv.now_utc()):
                with st.expander("➕ Work on another problem statement (same or another track)"):
                    st.caption(f"Same team, same account. Each problem statement gets its own {policy['submission']['max_attempts']} attempts and its own result.")
                    d, ps = choose_ps(cat, {e["ps_id"] for e in entries}, "add")
                    if ps:
                        ok = st.checkbox("I understand this addition cannot be removed later.", key="add_ok")
                        if st.button("Add this problem statement", disabled=not ok) and run(sv.add_problem_statement, conn, policy, user, d["id"], ps["id"],
                                                                                            ok="Added. Choose it on the 📤 Submit page."):
                            refresh()
        else:
            if not cat:
                st.warning("No tracks are open yet. Please check back after the administrator uploads them.")
                st.stop()
            st.markdown("#### Choose your track and problem statement")
            if sv.max_entries(policy) > 1:
                st.caption(f"You can add up to {sv.max_entries(policy) - 1} more problem statement(s) later, each with its own attempts.")
            d, ps = choose_ps(cat, set(), "first")
            ok = st.checkbox("I understand this selection cannot be changed later.")
            if st.button("Confirm and lock", disabled=not (ok and ps)) and run(sv.select_problem_statement, conn, user, d["id"], ps["id"], policy=policy, ok="Selection locked."):
                refresh()

elif page == "Submit":
    st.header("Submit abstract")
    if not user.team_id:
        st.warning("Register a team first.")
        st.stop()
    progress_tracker(user.team_id)
    entries = sv.team_entries(conn, user.team_id)
    if not entries:
        st.warning("Lock your problem statement on 👥 My Team first.")
        st.stop()
    if len(entries) > 1:
        sel = st.radio("Submit for which problem statement?", entries, horizontal=True, key="sub_ps",
                       format_func=lambda e: f"{e['ps_id']} · {STATUS_LABEL.get(e['last_status'], e['last_status'])} · {e['attempts_used']}/{policy['submission']['max_attempts']}")
    else:
        sel = entries[0]
    ps_id = sel["ps_id"]
    st.caption(f"Problem statement: **{sel['ps_id']} — {sel['ps_title']}** ({sel['track']})")
    okg, code, msg, used = sv.submission_gate(conn, policy, user.team_id, sv.now_utc(), ps_id)
    c = st.columns(4)
    c[0].metric("Attempts used", f"{used} / {policy['submission']['max_attempts']}")
    c[1].metric("Remaining", policy["submission"]["max_attempts"] - used)
    c[2].metric("Deadline", sv.deadline(policy).strftime("%d %b %Y %H:%M"))
    c[3].metric("Time left", left_until(sv.deadline(policy)))
    (st.info if okg else st.warning)(msg)
    v = policy["validation"]
    with st.expander("What your abstract is checked for", expanded=used == 0):
        st.markdown(
            f"1. **Related to your track** — uses at least {v['relevance']['min_keyword_matches']} of the track / problem-statement keywords\n"
            f"2. **Format** — headings: {', '.join(f'**{h}**' for h in v['required_sections'])}"
            + (f", each with at least {v.get('min_words_per_section', 0)} words" if v.get("min_words_per_section") else "") + "\n"
            f"3. **Length** — {v['min_words']}–{v['max_words']} words in the complete document\n"
            "4. **Language** — English\n"
            "5. **Matches your problem statement** — compared with every problem statement in the database\n"
            "6. **Original** — a copy of another team's submission is rejected; re-uploading your own previous file is blocked (no attempt used)\n"
            f"\nFiles: {', '.join(policy['submission']['allowed_extensions'])}, max {policy['submission']['max_file_mb']} MB.")
    if policy.get("evaluation", {}).get("blind_review", True):
        st.info("🙈 Abstracts are evaluated **blind**: do not write your team name, member names or college inside the document.")
    tf = setup.template_file(policy)
    if tf:
        st.download_button(f"⬇ Download the official template ({tf[0]})", tf[1], tf[0])
    with st.expander("🔍 Check my draft first — free, does NOT use an attempt", expanded=True):
        st.caption(f"Runs the same automatic checks and shows what to fix. Nothing is submitted or stored ({sv.DRAFT_CHECKS_PER_DAY} checks per team per day).")
        dgen = st.session_state.get("draft_gen", 0)
        dfile = st.file_uploader("Draft", type=[e.strip(".") for e in policy["submission"]["allowed_extensions"]], key=f"draft_up_{dgen}")
        if st.button("Check draft", disabled=not dfile):
            with st.spinner("Checking…"):
                dr = run(sv.check_draft, conn, policy, user, dfile.name, dfile.getvalue(), embedder=client if model_status()["embed_ok"] else None, ps_id=ps_id)
            if dr:
                {"pass": st.success, "review": st.warning, "fix": st.error}[dr["verdict"]](
                    {"pass": "✅ This draft would pass all automatic checks. You can submit it below.",
                     "review": "🧑‍⚖️ This draft would be sent to a human reviewer (e.g. similarity). It is not rejected, but check the notes.",
                     "fix": "✏️ Fix the items marked ❌ before submitting, so you don't lose an attempt."}[dr["verdict"]]
                    + f" · {dr['word_count']} words · {dr['checks_left']} draft check(s) left today")
                for r in dr["results"]:
                    icon = "✅" if r["result"] == "pass" else "⚠️" if r["result"] == "warn" or r["review_flag"] else "❌" if r["result"] == "fail" else "➖"
                    st.markdown(f"{icon} **{r['check_id']}** — {r['message']}" + (f"  \n  ↳ _{r['guidance']}_" if r["guidance"] and r["result"] != "pass" else ""))
    gen = st.session_state.get("uploader_gen", 0)
    f = st.file_uploader("Abstract / PPT", type=[e.strip(".") for e in policy["submission"]["allowed_extensions"]], key=f"abs_up_{gen}")
    confirm = st.checkbox("I confirm this is a formal submission and will consume an attempt.", key=f"abs_ok_{gen}")
    if st.button("Submit", disabled=not (f and confirm)):
        with st.spinner("Validating…"):
            ms = model_status()
            rep = run(sv.submit, conn, policy, settings.storage_dir, user, f.name, f.getvalue(), embedder=client if ms["embed_ok"] else None, ps_id=ps_id)
        if rep:  # rerun so the attempt counters above reflect this submission
            st.session_state["last_report"] = rep
            st.session_state["uploader_gen"] = st.session_state.get("uploader_gen", 0) + 1
            refresh()
    if (last := st.session_state.pop("last_report", None)) is not None:
        st.subheader("Result of your submission")
        show_report(last)

elif page == "History":
    st.header("Submission history")
    msgs = sv.team_messages(conn, user)
    if msgs:
        st.subheader(f"📬 Messages from the organisers ({len(msgs)})")
        for m in msgs:
            about = f" · about submission #{m['submission_id']}" if m["submission_id"] else ""
            with st.expander(f"{m['category']}: {m['subject']} — {m['created_at'][:16].replace('T', ' ')} UTC{about}", expanded=m is msgs[0]):
                st.write(m["body"])
        st.divider()
    q = st.text_input("Search (file name or status)")
    rows = sv.list_submissions(conn, user, search=q)
    if not rows:
        st.info("No submissions yet.")
    for r in rows:
        with st.expander(f"{r['ps_id']} · attempt {r['attempt_no']} · {r['file_name']} · {STATUS_LABEL.get(r['status'], r['status'])}"):
            show_report(sv.report(conn, user, r["id"]))

elif page == "Results":
    st.header("Evaluation results")
    results = ev.team_results(conn, policy, user)
    done = {r["ps_id"] for r in results}
    for e in sv.team_entries(conn, user.team_id) if user.team_id else []:
        if e["ps_id"] in done:
            continue
        if e["last_status"] == "ACCEPTED_FOR_EVALUATION":
            st.info(f"✅ {e['ps_id']}: accepted for evaluation and with the reviewers. You will get an email when results are released.")
        else:
            st.caption(f"{e['ps_id']}: results appear here after the abstract has been evaluated and results are released.")
    if not results and not user.team_id:
        st.info("Results appear here after your abstract has been evaluated and the organisers release the results.")
    for res in results:
        with st.container(border=True):
            (st.success if res["outcome"] == "shortlisted" else st.warning if res["outcome"] == "waitlisted" else st.error)(
                f"**{res['label']}** · {res['ps_id']} — {res['ps_title'] or ''} · abstract {res['paper']}")
            if res["mean_score"] is not None:
                st.metric("Average reviewer score", f"{res['mean_score']} / 100")
            if res["note"]:
                st.markdown("**Message from the organisers**")
                st.write(res["note"])
            for i, c in enumerate(res["comments"], 1):
                st.markdown(f"**Reviewer {i}**")
                st.write(c)

elif page == "Ask the Incubation Team":
    st.header("📨 Ask the Incubation Team")
    st.caption("Your message goes to the incubation team by email. You get a confirmation with a reference number, and the reply arrives by email and here.")
    with st.form("newq", clear_on_submit=True):
        c = st.columns([1, 2])
        cat_q = c[0].selectbox("Topic", qs.CATEGORIES)
        subj = c[1].text_input("Subject", max_chars=150)
        body = st.text_area("Your message", height=160, max_chars=5000,
                            help="Include your team code or abstract code (e.g. P-0007) if it is about a specific submission.")
        if st.form_submit_button("Send to the incubation team", type="primary") and run(qs.send_query, conn, policy, user, cat_q, subj, body,
                                                                                        ok="Sent. A confirmation email is on its way."):
            pass
    mine = qs.my_queries(conn, user)
    if mine:
        st.subheader(f"My queries ({len(mine)})")
    for q in mine:
        badge = {"open": "🟡 Waiting for reply", "answered": "🟢 Answered", "closed": "⚪ Closed"}[q["status"]]
        with st.expander(f"{q['ref']} · {q['subject']} · {badge}", expanded=q is mine[0]):
            for m in q["messages"]:
                who = "🏢 Incubation team" if m["from_staff"] else "🙋 You"
                st.markdown(f"**{who}** · {m['at'][:16].replace('T', ' ')} UTC")
                st.write(m["body"])
            if q["status"] != "closed":
                fu = st.text_area("Add a message to this query", key=f"fu{q['id']}")
                if st.button("Send", key=f"fub{q['id']}") and run(qs.follow_up, conn, policy, user, q["id"], fu, ok="Sent."):
                    refresh()

# ---------------- reviewer / admin ----------------
elif page == "My Reviews":
    st.header("📝 My review assignments")
    ec = policy["evaluation"]
    if not ev.has_pledged(conn, user.id):
        with st.container(border=True):
            st.markdown("#### 🔒 Reviewer confidentiality pledge")
            st.write(ev.PLEDGE)
            if st.checkbox("I agree", key="pledge_ok") and st.button("Confirm pledge", type="primary") and run(ev.pledge, conn, user, ok="Thank you."):
                refresh()
        st.stop()
    rows = ev.my_assignments(conn, policy, user)
    if not rows:
        st.info("No abstracts are assigned to you yet. You will get an email when the organisers assign one.")
        st.stop()
    c = st.columns(4)
    c[0].metric("Assigned", len(rows))
    c[1].metric("Submitted", sum(r["review_status"] == "submitted" for r in rows))
    c[2].metric("To do", sum(r["review_status"] != "submitted" for r in rows))
    c[3].metric("Overdue", sum(r["overdue"] for r in rows))
    st.dataframe(pd.DataFrame([{k: r[k] for k in ("paper", "track", "ps", "due", "status", "review_status", "total")} for r in rows]),
                 hide_index=True, width="stretch")
    aid = st.selectbox("Open an abstract", [r["assignment_id"] for r in rows],
                       format_func=lambda i: next(f"{r['paper']} · {r['track']} · due {r['due']} · {r['review_status']}" + (" ⚠ OVERDUE" if r["overdue"] else "")
                                                  for r in rows if r["assignment_id"] == i))
    r = next(x for x in rows if x["assignment_id"] == aid)
    with st.container(border=True):
        st.markdown(f"### {r['paper']}  \n**Track:** {r['track']}  \n**Problem statement:** {r['ps']}  \n**Team:** {r['team']} · **Words:** {r['word_count']} · **Due:** {r['due']}")
        if r["status"] == "assigned":
            st.warning("Step 1 — please accept this assignment, or decline it (e.g. conflict of interest: you know the team, same department, mentor…).")
            b = st.columns(2)
            if b[0].button("✅ Accept assignment", key=f"acc{aid}") and run(ev.respond, conn, policy, user, aid, True, ok="Accepted. You can now download and review."):
                refresh()
            with b[1].popover("Decline / conflict of interest"):
                why = st.text_area("Reason", key=f"why{aid}")
                coi = st.checkbox("I have a conflict of interest with this team", key=f"coi{aid}")
                if st.button("Decline", key=f"dec{aid}") and run(ev.respond, conn, policy, user, aid, False, why, coi, ok="Declined. The organisers were told."):
                    refresh()
        try:
            fname, fdata = ev.assignment_file(conn, policy, user, aid, settings.storage_dir) if r["status"] != "assigned" else (None, None)
            if fname:
                st.download_button(f"⬇ Download abstract ({fname})", fdata, fname, key=f"af{aid}", type="primary")
        except Exception as exc:
            st.error(f"File unavailable: {exc}")
    if r["status"] != "assigned":
        cur = ev.get_review(conn, user, aid) or {"scores": {}, "recommendation": None, "confidence": None, "comments_team": "", "comments_confidential": "", "status": "draft"}
        if r["locked"]:
            st.info("🔒 The organisers have recorded a decision; this review can no longer be changed.")
        st.markdown(f"#### Score the abstract ({ec['score_min']} = poor … {ec['score_max']} = excellent)")
        with st.form(f"rv{aid}"):
            scores = {}
            for cr in ec["criteria"]:
                scores[cr["id"]] = st.slider(f"{cr['name']} · weight {cr['weight']:g}", ec["score_min"], ec["score_max"],
                                             int(cur["scores"].get(cr["id"], (ec["score_min"] + ec["score_max"]) // 2)), help=cr.get("guide") or None)
            recs = list(ev.REC_LABEL)
            rec = st.radio("Overall recommendation", recs, horizontal=True, format_func=ev.REC_LABEL.get,
                           index=recs.index(cur["recommendation"]) if cur["recommendation"] in recs else 2)
            conf = st.select_slider("Your confidence", [1, 2, 3, 4, 5], value=cur["confidence"] or 3,
                                    format_func=lambda x: {1: "1 · guess", 2: "2 · low", 3: "3 · medium", 4: "4 · high", 5: "5 · expert"}[x])
            ct = st.text_area(f"Comments for the team (shared anonymously, ≥{ec['min_comment_chars']} characters): strengths, weaknesses, how to improve",
                              cur["comments_team"], height=160)
            cc = st.text_area("Confidential comments for the organisers only (optional)", cur["comments_confidential"], height=90)
            st.caption(f"Weighted score preview: **{ev.weighted_total(policy, scores)} / 100** (updates when you save)")
            b = st.columns(2)
            draft = b[0].form_submit_button("💾 Save draft", disabled=r["locked"] or cur["status"] == "submitted")
            final = b[1].form_submit_button("📨 Submit review" if cur["status"] != "submitted" else "📨 Submit revised review", type="primary", disabled=r["locked"])
            if (draft or final) and run(ev.save_review, conn, policy, user, aid, scores, rec, conf, ct, cc, final,
                                        ok="Review submitted. Thank you!" if final else "Draft saved."):
                refresh()
        others = ev.peer_reviews_visible_to(conn, policy, user, aid)
        if len(others) > 1:
            st.markdown("#### 💬 Other reviews of this abstract (visible after you submit yours)")
            st.dataframe(pd.DataFrame(others), hide_index=True, width="stretch")
    with st.expander("Declare a conflict of interest with this team later"):
        why2 = st.text_input("Reason", key=f"coi2{aid}")
        if st.button("Declare conflict and withdraw from this abstract", key=f"coib{aid}"):
            tid = conn.execute("SELECT team_id FROM submissions WHERE id=?", (r["submission_id"],)).fetchone()[0]
            if run(ev.declare_conflict, conn, user, user.id, tid, why2, ok="Conflict recorded; the assignment was withdrawn."):
                refresh()

elif page == "Peer Review":
    sv.authorize(conn, user, "admin", action="peer_review_page")
    ec = policy["evaluation"]
    st.header("🎯 Peer review of accepted abstracts")
    st.caption(f"{ec['reviewers_per_submission']} reviewer(s) per abstract · {ec['review_days']} days to review · "
               f"{'blind' if ec['blind_review'] else 'open'} review · change these in ⚙️ Programme Setup → Peer review rubric.")
    subs = ev.evaluable(conn, user, policy)
    pool = ev.reviewer_pool(conn, user)
    c = st.columns(5)
    c[0].metric("Accepted abstracts", len(subs))
    c[1].metric("Fully assigned", sum(x["needed"] == 0 for x in subs))
    c[2].metric("Reviews done", sum(x["reviews_done"] for x in subs))
    c[3].metric("Decided", sum(bool(x["decision"]) for x in subs))
    c[4].metric("Released", sum(x["released"] for x in subs))
    t1, t2, t3, t4, t5 = st.tabs(["1 · Reviewers", "2 · Assign", "3 · Progress", "4 · Decisions", "5 · Results & release"])
    tracks = {d["id"]: d["name"] for d in sv.catalog(conn)}
    with t1:
        if not pool:
            st.warning("No reviewer accounts yet. Create them on 🔑 Users with role **reviewer** and an email address.")
        else:
            st.dataframe(pd.DataFrame([{**p, "expertise": ", ".join(tracks.get(e, e) for e in p["expertise"])} for p in pool]), hide_index=True, width="stretch")
            with st.form("rprof"):
                st.markdown("**Reviewer profile** — expertise tracks are preferred by auto-assign; max load = most abstracts at once")
                rid = st.selectbox("Reviewer", [p["id"] for p in pool], format_func=lambda i: next(p["username"] for p in pool if p["id"] == i))
                cur = next(p for p in pool if p["id"] == rid)
                exp = st.multiselect("Expertise (tracks)", list(tracks), default=cur["expertise"], format_func=lambda t: tracks.get(t, t))
                cc = st.columns(2)
                ml = cc[0].number_input("Max load", 0, 100, cur["max_load"])
                av = cc[1].toggle("Available for new assignments", cur["available"])
                if st.form_submit_button("Save profile") and run(ev.set_profile, conn, user, rid, exp, int(ml), av, ok="Saved."):
                    refresh()
            st.markdown("**Conflicts of interest** (a conflicted reviewer is never assigned to that team; the same email as a team member is detected automatically)")
            teams = [dict(x) for x in conn.execute("SELECT id, team_code, name FROM teams ORDER BY team_code")]
            if teams:
                with st.form("coi"):
                    cc = st.columns(3)
                    crv = cc[0].selectbox("Reviewer", [p["id"] for p in pool], format_func=lambda i: next(p["username"] for p in pool if p["id"] == i), key="coir")
                    ctm = cc[1].selectbox("Team", [x["id"] for x in teams], format_func=lambda i: next(f"{x['team_code']} · {x['name']}" for x in teams if x["id"] == i))
                    cwhy = cc[2].text_input("Reason")
                    if st.form_submit_button("Record conflict") and run(ev.declare_conflict, conn, user, crv, ctm, cwhy, ok="Conflict recorded."):
                        refresh()
            cl = ev.conflicts(conn, user)
            if cl:
                st.dataframe(pd.DataFrame(cl), hide_index=True, width="stretch")
    with t2:
        if not subs:
            st.info("No abstracts are accepted for evaluation yet.")
        else:
            st.dataframe(pd.DataFrame([{k: x[k] for k in ("paper", "team_code", "track", "ps_id", "reviewers", "assigned", "needed", "reviews_done", "decision")} for x in subs]),
                         hide_index=True, width="stretch")
            if st.button(f"⚡ Auto-assign every abstract up to {ec['reviewers_per_submission']} reviewers", type="primary"):
                res = run(ev.auto_assign, conn, policy, user)
                if res:
                    st.success(f"{res['assigned']} assignment(s) made; reviewers were emailed.")
                    if res["short"]:
                        st.warning(f"Not enough free reviewers for: {', '.join(res['short'])}. Add reviewers or raise max load.")
            st.markdown("#### 🎯 Assign by track → problem statement")
            tracks_with = {}
            for x in subs:
                tracks_with.setdefault(x["domain_id"], []).append(x)
            tsel = st.selectbox("1 · Track", sorted(tracks_with), key="as_track",
                                format_func=lambda d: f"{d} — {tracks.get(d, d)} · {len(tracks_with[d])} abstract(s) · "
                                                      f"{sum(x['needed'] > 0 and not x['decision'] for x in tracks_with[d])} need reviewers")
            by_ps = {}
            for x in tracks_with[tsel]:
                by_ps.setdefault(x["ps_id"], []).append(x)
            psel = st.selectbox("2 · Problem statement", sorted(by_ps, key=lambda k: (-sum(x["needed"] > 0 for x in by_ps[k]), k)), key="as_ps",
                                format_func=lambda k: f"{k} — {by_ps[k][0]['ps_title'] or ''} · {len(by_ps[k])} abstract(s) · "
                                                      f"{sum(x['needed'] > 0 and not x['decision'] for x in by_ps[k])} unassigned")
            only_open = st.toggle("Show only abstracts that still need reviewers", True, key="as_open")
            plist = [x for x in by_ps[psel] if not x["decision"] and (x["needed"] > 0 or not only_open)]
            if not plist:
                st.success("✅ Every abstract for this problem statement has its reviewers.")
            else:
                st.dataframe(pd.DataFrame([{"Abstract": x["paper"], "Team": x["team_code"], "Reviewers": x["reviewers"],
                                            "Assigned": x["assigned"], "Still needed": x["needed"]} for x in plist]), hide_index=True, width="stretch")
                sids = st.multiselect("3 · Abstract(s) to assign", [x["submission_id"] for x in plist], default=[x["submission_id"] for x in plist if x["needed"] > 0][:1],
                                      format_func=lambda i: next(f"{x['paper']} · {x['team_code']} · needs {x['needed']}" for x in plist if x["submission_id"] == i), key="as_sids")
                team_ids = {i: conn.execute("SELECT team_id FROM submissions WHERE id=?", (i,)).fetchone()[0] for i in sids}
                lab = {p["id"]: f"{p['username']} · load {p['load']}/{p['max_load']}" + (" · ⭐ track expert" if tsel in p["expertise"] else "")
                       + (" · ⛔ conflict" if any(ev.conflict_reason(conn, p["id"], tid) for tid in team_ids.values()) else "")
                       + ("" if p["available"] else " · unavailable") for p in pool}
                opts = sorted([p["id"] for p in pool if p["active"]], key=lambda i: (tsel not in next(p["expertise"] for p in pool if p["id"] == i),
                                                                                      next(p["load"] for p in pool if p["id"] == i)))
                picks = st.multiselect("4 · Reviewers (experts and least-loaded first)", opts, format_func=lambda i: lab[i], key="as_rev")
                due = st.date_input("5 · Review due date", sv.now_utc().date() + pd.Timedelta(days=ec["review_days"]), key="as_due")
                if st.button(f"Assign {len(picks)} reviewer(s) to {len(sids)} abstract(s) and email them", type="primary", disabled=not (sids and picks)):
                    okn = sum(bool(run(ev.assign, conn, policy, user, sid, rid, due)) for sid in sids for rid in picks)
                    if okn:
                        st.success(f"{okn} assignment(s) made; reviewers were emailed.")
                        refresh()
    with t3:
        rows = ev.all_assignments(conn, user)
        if not rows:
            st.info("No assignments yet.")
        else:
            st.dataframe(pd.DataFrame(rows).drop(columns=["submission_id"]), hide_index=True, width="stretch")
            b = st.columns(2)
            if b[0].button("📧 Send due / overdue reminders now"):
                n = run(ev.send_due_reminders, conn, policy, sv.now_utc())
                st.success(f"{n or 0} reminder(s) queued.") if n is not None else None
            with b[1].form("wd"):
                openr = [x for x in rows if x["status"].split(" ")[0] in ev.OPEN]
                if openr:
                    wid = st.selectbox("Withdraw an assignment (to reassign it)", [x["assignment_id"] for x in openr],
                                       format_func=lambda i: next(f"{x['paper']} · {x['reviewer']} · {x['status']}" for x in openr if x["assignment_id"] == i))
                    wr = st.text_input("Reason", "reassigning")
                    if st.form_submit_button("Withdraw") and run(ev.withdraw, conn, user, wid, wr, ok="Withdrawn; the reviewer was told."):
                        refresh()
                else:
                    st.caption("Nothing open to withdraw.")
                    st.form_submit_button("Withdraw", disabled=True)
    with t4:
        ready = [x for x in subs if x["reviews_done"]]
        if not ready:
            st.info("Decisions become possible once at least one review is submitted.")
        else:
            sid = st.selectbox("Abstract", [x["submission_id"] for x in ready],
                               format_func=lambda i: next(f"{x['paper']} · {x['team_code']} · {x['reviews_done']} review(s) · mean {x['mean_score']}"
                                                          + (" · ⚠ disagreement" if x["disagreement"] else "") + (f" · decided: {x['decision']}" if x["decision"] else "")
                                                          for x in ready if x["submission_id"] == i))
            d = ev.submission_reviews(conn, policy, user, sid)
            a = d["aggregate"]
            st.markdown(f"**{d['paper']}** · {d['team_code']} · {d['team_name']} · {d['track']} · {d['ps']}")
            c = st.columns(4)
            c[0].metric("Mean score", f"{a['mean']} / 100")
            c[1].metric("Confidence-weighted", a["weighted_mean"])
            c[2].metric("Spread", a["spread"])
            c[3].metric("Reviews", a["count"])
            if a["disagreement"]:
                st.warning("⚠ Reviewers disagree (large score spread or mixed accept/reject). Read the reviews closely or add a reviewer.")
            crit = {cr["id"]: cr["name"] for cr in ec["criteria"]}
            st.dataframe(pd.DataFrame([{"reviewer": r["username"], "status": r["status"], **{crit.get(k, k): v for k, v in r["scores"].items()},
                                        "total": r["total"], "recommendation": ev.REC_LABEL.get(r["recommendation"], r["recommendation"]),
                                        "confidence": r["confidence"]} for r in d["reviews"]]), hide_index=True, width="stretch")
            for r in d["reviews"]:
                if r["status"] == "submitted":
                    with st.expander(f"{r['username']} · {r['total']}/100 · {ev.REC_LABEL.get(r['recommendation'])}"):
                        st.markdown("**For the team:**")
                        st.write(r["comments_team"])
                        if r["comments_confidential"]:
                            st.markdown("**Confidential (organisers only):**")
                            st.write(r["comments_confidential"])
            try:
                fname, fdata = sv.submission_file(conn, user, sid, settings.storage_dir)
                st.download_button(f"⬇ Open the abstract ({fname})", fdata, fname, key=f"evf{sid}")
            except Exception as exc:
                st.caption(f"File unavailable: {exc}")
            if d["decisions"]:
                st.caption("Decision history: " + " → ".join(f"{x['outcome']} ({x['decided_by_name']}, {x['decided_at'][:10]}{', released' if x['released_at'] else ''})"
                                                             for x in reversed(d["decisions"])))
            with st.form(f"evdec{sid}"):
                outc = st.radio("Decision", list(ev.OUTCOME_LABEL), horizontal=True, format_func=ev.OUTCOME_LABEL.get)
                note = st.text_area("Message to the team (optional, included in the result email)")
                share = st.checkbox("Share the reviewers' comments for the team (anonymised as Reviewer 1, 2…)", True)
                now_rel = st.checkbox("Email the team now (otherwise release later in tab 5, all teams at once)", False)
                if st.form_submit_button("Record decision", type="primary") and run(ev.decide, conn, policy, user, sid, outc, note, share, now_rel,
                                                                                     ok="Decision recorded." + (" The team was emailed." if now_rel else "")):
                    refresh()
    with t5:
        board = ev.leaderboard(conn, policy, user)
        if not board:
            st.info("Rankings appear once reviews are submitted.")
        else:
            df = pd.DataFrame([{k: x[k] for k in ("track", "rank_in_track", "paper", "team_code", "team_name", "ps_id", "mean_score", "spread",
                                                   "reviews_done", "recommendations", "decision", "released")} for x in board])
            st.dataframe(df, hide_index=True, width="stretch")
            st.download_button("⬇ Export results (CSV)", df.to_csv(index=False), "evaluation_results.csv", "text/csv")
            pending = sum(1 for x in board if x["decision"] and not x["released"])
            st.markdown(f"**{pending}** decision(s) recorded but not yet sent to teams.")
            okr = st.checkbox("I have checked every decision and want to email all pending results now")
            if st.button("📣 Release all pending results", disabled=not (okr and pending), type="primary"):
                n = run(ev.release, conn, policy, user, None)
                if n is not None:
                    st.success(f"Results emailed to {n} team(s).")

elif page == "Review Queue":
    st.header("Review queue")
    c = st.columns(2)
    flag = c[0].selectbox("Filter by flag", ["", "SIMILARITY", "NEAR_DUPLICATE", "DUPLICATE_SUBMISSION", "DOMAIN_RELEVANCE", "PS_SIMILARITY", "SEMANTIC_SIMILARITY"])
    only_open = c[1].toggle("Open cases only", True)
    rows = sv.review_queue(conn, user, only_open, flag)
    st.caption(f"{len(rows)} case(s) · deadline {sv.deadline(policy).isoformat()}")
    approved = policy["review"]["approved_rejection_policies"]
    for r in rows:
        with st.expander(f"#{r['submission_id']} · {r['team_code']} · attempt {r['attempt_no']} · {', '.join(r['reasons'])} · lexical {r['max_lexical']}"):
            show_report(sv.report(conn, user, r["submission_id"]))
            if r["case_status"] == "open":
                with st.form(f"dec{r['submission_id']}"):
                    outcome = st.radio("Decision", ["cleared", "corrections_requested", "rejected"], horizontal=True)
                    reason = st.text_area(f"Reason (required, ≥{policy['review']['reason_min_chars']} chars)")
                    pref = st.selectbox("Approved rejection policy (required for rejection)", [""] + [p["id"] for p in approved])
                    if not approved:
                        st.caption("No rejection policy has been approved by the administrator; rejection is unavailable.")
                    if st.form_submit_button("Record decision") and run(sv.decide, conn, policy, user, r["submission_id"], outcome, reason, pref or None, ok="Decision recorded."):
                        refresh()

elif page == "Submissions":
    st.header("All submissions")
    q = st.text_input("Search (team, file, status)")
    rows = sv.list_submissions(conn, user, search=q)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if rows:
        sid = st.selectbox("Open report", [r["id"] for r in rows],
                           format_func=lambda i: next(f"#{r['id']} · {r['team_code']} · attempt {r['attempt_no']} · {r['status']}" for r in rows if r["id"] == i))
        show_report(sv.report(conn, user, sid))
        st.markdown("#### ✉️ Send an update to this team about this submission")
        st.caption("Feedback on the draft, a request, or good news. The team gets an email (contact + members) and sees it in the portal under History.")
        with st.form(f"upd_{sid}", clear_on_submit=True):
            cat = st.selectbox("Type", sv.UPDATE_CATEGORIES[:4])
            subj = st.text_input("Subject", placeholder="e.g. Please strengthen the Methodology section")
            msg = st.text_area("Message", height=160, placeholder="Write your comments for the team…")
            if st.form_submit_button("Send update") and run(sv.send_submission_update, conn, policy, user, sid, cat, subj, msg,
                                                             ok="Update sent. The team has been emailed and can read it in the portal."):
                pass
        sent = sv.team_messages(conn, user, submission_id=sid)
        if sent:
            st.markdown("**Updates already sent about this submission**")
            for m in sent:
                st.write(f"**{m['category']}: {m['subject']}** · {m['created_at'][:16].replace('T', ' ')} UTC · by {m['sent_by']}")
                st.caption(m["body"])
        st.download_button("Export all (CSV)", pd.DataFrame(rows).to_csv(index=False), "submissions.csv", "text/csv")

elif page == "Dashboard":
    sv.authorize(conn, user, "admin", action="dashboard")
    st.header("Administrator dashboard")
    ps, cs = persist.status(), cloud.status()
    if cs["enabled"]:
        (st.error if cs["last_error"] else st.caption)(f"☁️ Data saved to Hugging Face dataset `{cs['repo']}` · last upload: {cs['last_push'] or 'no changes yet'}"
                                                       + (f" · ⚠️ {cs['last_error']}" if cs["last_error"] else ""))
    elif ps["enabled"]:
        st.caption(f"💾 {ps['message']}")
    elif os.environ.get("STREAMLIT_SHARING_MODE") or os.environ.get("HOSTNAME", "") == "streamlit":
        st.error("⚠️ Data is NOT being saved permanently. Add HF_TOKEN and HF_DATA_REPO to the app's Secrets (CLOUD_SETUP.md, Part D).")
    elif os.environ.get("SPACE_ID") or os.environ.get("PERSIST_DIR"):
        st.error("⚠️ Data is NOT being saved permanently: this server's disk is wiped on restart. "
                 "Add the Space secrets HF_TOKEN and HF_DATA_REPO (HF_SETUP.md, Part D), then restart the Space.")
    rows_all = stats.entry_rows(conn, user)
    h = stats.headline(conn, rows_all)
    c = st.columns(6)
    c[0].metric("Teams", h["teams"], help=f"{h['teams_without_ps']} have not locked a problem statement yet")
    c[1].metric("Team entries", h["entries"], help="team × problem statement")
    c[2].metric("Submitted", h["submitted"])
    c[3].metric("Accepted", h["accepted"])
    c[4].metric("Reviews done", h["reviews_done"])
    c[5].metric("Open queries", h["open_queries"])
    if h["teams_without_ps"]:
        st.caption(f"ℹ️ {h['teams_without_ps']} registered team(s) have not locked a problem statement and are not counted in the tables below.")
    with st.container(border=True):
        st.markdown("**Filters**")
        f = st.columns([2, 2, 2, 2])
        ftr = f[0].multiselect("Track", sorted({r["track"] for r in rows_all}), placeholder="All tracks")
        fps = f[1].multiselect("Problem statement", sorted({r["ps_id"] for r in rows_all if not ftr or r["track"] in ftr}), placeholder="All")
        fstg = f[2].multiselect("Stage", stats.STAGES, placeholder="All stages")
        fdec = f[3].multiselect("Decision", list(stats.DECISIONS.values()) + ["(none yet)"], placeholder="All")
        fq = st.text_input("Search team code, team name, abstract code or title")
    rows = [r for r in rows_all if (not ftr or r["track"] in ftr) and (not fps or r["ps_id"] in fps) and (not fstg or r["stage"] in fstg)
            and (not fdec or (r["decision"] or "(none yet)") in fdec)
            and (not fq or fq.lower() in " ".join(str(r[k]) for k in ("team_code", "team_name", "paper", "ps_title", "ps_id")).lower())]
    if not rows_all:
        st.info("No team has locked a problem statement yet. Statistics appear here as teams register and submit.")
    else:
        t1, t2, t3, t4 = st.tabs(["📊 By track", "🧩 By problem statement", "📄 All entries", "🩺 System health"])
        with t1:
            ts = stats.track_summary(rows, policy)
            tot = stats.totals(rows, policy, "Track")
            df = pd.DataFrame(ts + ([tot] if len(ts) > 1 else []))
            st.dataframe(df, hide_index=True, width="stretch",
                         column_config={"Review completion %": st.column_config.ProgressColumn("Review completion", min_value=0, max_value=100, format="%d%%")})
            st.download_button("⬇ Export track summary (CSV)", df.to_csv(index=False), "track_summary.csv", "text/csv")
            chart = pd.DataFrame([{**{"Track": r["Track"]}, **{stg: r[stg] for stg in stats.STAGES}} for r in ts]).set_index("Track")
            st.markdown("**Where every entry stands, by track**")
            st.bar_chart(chart, horizontal=True, stack=True)
        with t2:
            pss = stats.ps_summary(rows, policy)
            dfp = pd.DataFrame(pss)
            st.dataframe(dfp, hide_index=True, width="stretch",
                         column_config={"Review completion %": st.column_config.ProgressColumn("Review completion", min_value=0, max_value=100, format="%d%%")})
            st.download_button("⬇ Export problem-statement summary (CSV)", dfp.to_csv(index=False), "ps_summary.csv", "text/csv")
            pop = dfp.set_index("Problem statement")[["Entries"]].sort_values("Entries", ascending=False)
            st.markdown("**Most chosen problem statements**")
            st.bar_chart(pop, horizontal=True)
        with t3:
            dfe = pd.DataFrame([{"Abstract": r["paper"], "Track": r["track"], "PS": r["ps_id"], "Team": r["team_code"], "Team name": r["team_name"],
                                 "Stage": r["stage"], "Attempts": r["attempts"], "Submitted": r["submitted_at"], "Reviewers": r["reviewers"],
                                 "Reviews done": r["reviews_done"], "Avg score": r["mean_score"], "Decision": r["decision"],
                                 "Released": "✅" if r["released"] else ""} for r in rows])
            st.caption(f"{len(dfe)} of {len(rows_all)} entries shown")
            st.dataframe(dfe, hide_index=True, width="stretch")
            st.download_button("⬇ Export entries (CSV)", dfe.to_csv(index=False), "entries.csv", "text/csv")
        with t4:
            m = workflow_summary(conn)
            c = st.columns(3)
            c[0].metric("Review backlog", m["review_backlog"])
            c[1].metric("Avg processing (ms)", m["avg_processing_ms"] or "-")
            c[2].metric("Deadline failures", m["deadline_missed"])
            for title, key in [("Failed checks", "failed_checks"), ("Blocked uploads (no attempt used)", "blocked_submissions"), ("Emails", "notifications")]:
                st.markdown(f"**{title}**")
                if m[key]:
                    st.bar_chart(pd.Series(m[key]).rename(index=str))
                else:
                    st.caption("No data yet.")

elif page == "Users":
    sv.authorize(conn, user, "admin", action="users_page")
    st.header("Users")
    with st.form("nu"):
        un, pw = st.text_input("Username"), st.text_input("Temporary password", type="password")
        role = st.selectbox("Role", ["reviewer", "admin", "student"])
        em = st.text_input("Email (reviewers/admins receive review alerts here)")
        if st.form_submit_button("Create user"):
            run(sv.admin_create_user, conn, user, un, pw, role, em, policy, ok="User created.")
    f = st.columns([2, 1])
    uq = f[0].text_input("Search username, email or team")
    ur = f[1].selectbox("Role filter", ["", "student", "reviewer", "admin"], format_func=lambda x: x or "All roles")
    ulist = sv.list_users(conn, user, uq, ur)
    st.dataframe(pd.DataFrame([{"Username": u["username"], "Role": u["role"], "Email": u["email"] or "", "Team": u["team_code"] or "",
                                "Active": "✅" if u["active"] else "⛔", "Last sign-in": (u["last_login"] or "")[:16].replace("T", " "),
                                "Created": u["created_at"][:10]} for u in ulist]), hide_index=True, width="stretch")
    if ulist:
        st.markdown("#### ✏️ Edit or remove a user")
        uid = st.selectbox("User", [u["id"] for u in ulist], format_func=lambda i: next(f"{u['username']} ({u['role']})" for u in ulist if u["id"] == i))
        cu = next(u for u in ulist if u["id"] == uid)
        with st.form(f"edit{uid}"):
            c = st.columns(3)
            e_email = c[0].text_input("Email (empty = remove)", cu["email"] or "")
            e_role = c[1].selectbox("Role", ["student", "reviewer", "admin"], index=["student", "reviewer", "admin"].index(cu["role"]))
            e_active = c[2].toggle("Active (can sign in)", bool(cu["active"]))
            e_pw = st.text_input("Reset password (leave empty to keep)", type="password")
            if st.form_submit_button("Save changes", type="primary"):
                r = run(sv.update_user, conn, policy, user, uid, email=e_email, role=e_role, active=e_active, new_password=e_pw or None)
                if r:
                    st.success("Saved: " + ", ".join(r["changed"]) + ". The user was emailed." if r["changed"] else "Nothing changed.")
        with st.expander("🗑️ Remove this user"):
            st.caption("Only accounts with no team, submissions, reviews or queries can be deleted. Others can be deactivated above "
                       "(they can no longer sign in, and every record keeps its author).")
            okd = st.checkbox(f"Yes, permanently delete '{cu['username']}'", key=f"del_ok{uid}")
            if st.button("Delete user", disabled=not okd, key=f"del{uid}") and run(sv.delete_user, conn, user, uid, ok="User deleted."):
                refresh()

elif page == "Knowledge Base":
    sv.authorize(conn, user, "admin", action="kb_page")
    st.header("Knowledge base & models")
    with st.expander("➕ Upload a guideline / FAQ / policy document", expanded=False):
        with st.form("kb_up", clear_on_submit=True):
            kf = st.file_uploader("Document (PDF, DOCX, PPTX, MD, TXT)", type=["pdf", "docx", "pptx", "md", "txt"])
            c = st.columns(4)
            kver = c[0].text_input("Version", "1.0")
            ktype = c[1].selectbox("Type", ["guidelines", "faq", "deadline_resubmission_policy", "originality_policy", "evaluation_criteria",
                                            "submission_template", "reviewer_guidelines", "clarification", "other"])
            kstat = c[2].selectbox("Status", ["approved", "draft"])
            kaud = c[3].selectbox("Visible to", ["student", "reviewer", "admin"], help="'student' = everyone; 'reviewer' = reviewers and admins only")
            if st.form_submit_button("Add to knowledge base") and kf:
                run(setup.add_kb_document, conn, user, settings.kb_dir, kf.name, kf.getvalue(), kver, ktype, kstat, kaud, ok="Added. Rebuild the index below.")
    docs = setup.kb_documents(settings.kb_dir)
    st.dataframe(pd.DataFrame(docs), hide_index=True, width="stretch")
    if docs:
        c = st.columns(3)
        kfile = c[0].selectbox("Document", [d["file"] for d in docs])
        knew = c[1].selectbox("Set status", ["approved", "draft", "retired"])
        c[2].write("")
        if c[2].button("Update status") and run(setup.set_kb_status, conn, user, settings.kb_dir, kfile, knew, ok="Status updated. Rebuild the index."):
            refresh()
    st.markdown("**Models**")
    st.json(model_status())
    stale = HybridIndex.needs_rebuild(settings.kb_dir, settings.index_dir)
    st.write("Index status:", "⚠️ missing or stale" if stale else "✅ up to date")
    if st.button("Rebuild index"):
        ms = model_status()
        idx = HybridIndex.build(settings.kb_dir, settings.index_dir, client if ms["embed_ok"] else None, policy["rag"]["chunk_words"], policy["rag"]["chunk_overlap"])
        sv.audit(conn, user, "kb_reindexed", details={"chunks": idx.meta["chunks"], "semantic": idx.meta["semantic"]})
        st.success(f"Indexed {idx.meta['chunks']} chunks · semantic={idx.meta['semantic']}")
    if (settings.index_dir / "meta.json").exists():
        st.json(json.loads((settings.index_dir / "meta.json").read_text()))

elif page == "Programme Setup":
    sv.authorize(conn, user, "admin", action="programme_setup")
    st.header("Programme setup")
    st.caption(f"Active rules version: {policy['version']}. Changes apply immediately to new submissions; earlier submissions keep the version they were checked with.")
    t1, t2, t3, t4, t5, t6, t7 = st.tabs(["Tracks & problem statements", "Abstract format / template", "Deadline & rules", "Validation checks",
                                          "Emails & reminders", "Peer review rubric", "AI keys"])
    with t7:
        st.markdown("Paste API keys from one or more AI providers. They are used **in this order**; when one is busy or fails, the next "
                    "takes over automatically and the failing one rests for a minute. Free keys: Groq, Google Gemini, OpenRouter, Hugging Face, "
                    "Mistral, Cerebras. Keys are stored in your private data and shown only as their last 4 characters.")
        provs = providers.list_providers(conn, user)
        if provs:
            st.dataframe(pd.DataFrame([{"#": i + 1, "Name": p_["name"], "Model": p_["model"], "Key": p_["api_key"], "On": "✅" if p_["enabled"] else "⏸",
                                        "Last OK": (p_["last_ok_at"] or "")[:16].replace("T", " "), "Last error": (p_["last_error"] or "")[:60],
                                        "Resting until": (p_["cooldown_until"] or "")[11:19]} for i, p_ in enumerate(provs)]), hide_index=True, width="stretch")
            pid = st.selectbox("Manage", [p_["id"] for p_ in provs], format_func=lambda i: next(f"{p_['name']} · {p_['model']}" for p_ in provs if p_["id"] == i))
            cur = next(p_ for p_ in provs if p_["id"] == pid)
            b = st.columns(5)
            if b[0].button("🧪 Test"):
                okp, msgp = providers.test_provider(conn, user, pid)
                (st.success if okp else st.error)(msgp)
            if b[1].button("⬆ Up"):
                providers.move_provider(conn, user, pid, True)
                refresh()
            if b[2].button("⬇ Down"):
                providers.move_provider(conn, user, pid, False)
                refresh()
            if b[3].button("⏸ Pause" if cur["enabled"] else "▶ Use"):
                providers.update_provider(conn, user, pid, enabled=not cur["enabled"])
                refresh()
            if b[4].button("🗑 Remove"):
                providers.delete_provider(conn, user, pid)
                refresh()
            with st.form(f"provupd{pid}"):
                c = st.columns(2)
                nm = c[0].text_input("Change model", cur["model"])
                nk = c[1].text_input("Replace key (optional)", type="password")
                if st.form_submit_button("Save") and run(providers.update_provider, conn, user, pid, model=nm, api_key=nk or None, ok="Saved."):
                    refresh()
        else:
            st.info("No AI keys yet. Without a key, the portal works fully; only AI-drafted query replies and the staff AI assistant are off.")
        st.markdown("**➕ Add a key**")
        preset = st.selectbox("Provider", list(providers.PRESETS), key="prov_preset")
        base_u, model_u, where = providers.PRESETS[preset]
        if where:
            st.caption(f"Get a key at {where}")
        with st.form("provadd", clear_on_submit=True):
            c = st.columns(3)
            pn = c[0].text_input("Name", preset if "Custom" not in preset else "")
            pb = c[1].text_input("API address (OpenAI-compatible)", base_u)
            pm = c[2].text_input("Model", model_u)
            pk = st.text_input("API key", type="password")
            if st.form_submit_button("Add key", type="primary") and run(providers.add_provider, conn, user, pn, pb, pk, pm, ok="Key added. Click Test to check it."):
                refresh()
    with t6:
        ec = policy["evaluation"]
        st.markdown("How accepted abstracts are evaluated by reviewers (like a conference programme committee).")
        with st.form("evset"):
            c = st.columns(3)
            k = c[0].number_input("Reviewers per abstract", 1, 5, int(ec["reviewers_per_submission"]))
            days = c[1].number_input("Days reviewers get", 1, 60, int(ec["review_days"]))
            mc = c[2].number_input("Min. characters of comments for the team", 0, 2000, int(ec["min_comment_chars"]))
            c = st.columns(3)
            blind = c[0].toggle("Blind review (reviewers don't see team names)", ec["blind_review"])
            share = c[1].toggle("Show average score to teams", ec["share_scores_with_team"])
            spread = c[2].number_input("Flag disagreement when scores differ by (points of 100)", 1, 100, int(ec["disagreement_spread"]))
            rem = st.text_input("Reviewer reminders: hours before the due date", ", ".join(str(h) for h in ec["reminder_hours_before_due"]))
            st.markdown("**Rubric** — scores from 1 to 5 per criterion; the weighted total is shown out of 100. Add or remove rows as you like.")
            crit = st.data_editor(pd.DataFrame(ec["criteria"])[["id", "name", "weight", "guide"]], num_rows="dynamic", hide_index=True, width="stretch")
            if st.form_submit_button("Save peer-review settings"):
                try:
                    hrs = [int(x) for x in rem.replace(";", ",").split(",") if x.strip()]
                except ValueError:
                    hrs = None
                    st.error("Reminder hours must be numbers separated by commas, e.g. 48, 24")
                if hrs is not None:
                    new = dict(ec, reviewers_per_submission=int(k), review_days=int(days), min_comment_chars=int(mc), blind_review=blind,
                               share_scores_with_team=share, disagreement_spread=float(spread), reminder_hours_before_due=hrs,
                               criteria=[{kk: ("" if pd.isna(vv) else vv) for kk, vv in r.items()} for r in crit.to_dict("records")])
                    if run(setup.update_settings, conn, user, {"evaluation": new}, base_policy, ok="Peer-review settings saved."):
                        refresh()
    with t1:
        st.markdown("Upload a **CSV, Excel (.xlsx) or JSON** file with one row per problem statement. "
                    "Required columns: `track_id, track_name, ps_id, ps_title`; optional: `track_description, ps_description, keywords` (separate keywords with `;`).")
        st.download_button("⬇ Download blank example (CSV)", setup.catalogue_template_csv(), "tracks_problem_statements_template.csv", "text/csv")
        cur = setup.catalogue_dataframe(conn)
        st.download_button("⬇ Download current catalogue (CSV)", cur.to_csv(index=False), "current_catalogue.csv", "text/csv")
        up = st.file_uploader("Catalogue file", type=["csv", "xlsx", "json"], key="cat_up")
        mode = st.radio("Import mode", ["Replace: hide tracks/PS not in this file", "Merge: add/update only"], horizontal=True)
        if up:
            rows, errs = setup.parse_catalogue(up.name, up.getvalue())
            if errs:
                st.error("Fix these problems and upload again:\n\n" + "\n".join(f"- {e}" for e in errs[:20]))
            else:
                st.success(f"Preview: {len({r['track_id'] for r in rows})} track(s), {len(rows)} problem statement(s)")
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                if st.button("Import catalogue") and run(setup.import_catalogue, conn, user, up.name, up.getvalue(), mode.startswith("Replace"), settings.kb_dir, ok="Catalogue imported."):
                    st.info("Rebuild the knowledge-base index (Knowledge Base page) so the assistant knows the new tracks.")
        st.markdown("**Current catalogue** (`ps_active = 0` → hidden from new selections; teams that locked it keep it)")
        st.dataframe(cur, hide_index=True, width="stretch")
    with t2:
        st.markdown("Upload the official abstract template (**DOCX, PPTX or PDF**). Its headings are detected and proposed as the required sections; "
                    "edit them, then save. Students can download the template from the Submit page.")
        st.download_button("⬇ Download an example template (DOCX) with the four required headings", setup.example_template_docx(),
                           "abstract_template_example.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        tf = setup.template_file(policy)
        if tf:
            st.caption(f"Current template: {tf[0]} (uploaded {policy['template']['file']['uploaded_at']})")
        tup = st.file_uploader("Template file", type=["docx", "pptx", "pdf"], key="tpl_up")
        detected = None
        if tup:
            try:
                detected = setup.detect_template_sections(tup.name, tup.getvalue())
                st.success(f"Detected {len(detected)} heading(s): {', '.join(detected) or 'none'}")
            except sv.WorkflowError as e:
                st.error(str(e))
        start = {h: [h.lower()] for h in detected} if detected else policy["validation"]["required_sections"]
        st.markdown("Required sections, one per line, as `Section name | alternative heading, alternative heading`")
        sec_text = st.text_area("Required sections", setup.sections_to_text(start), height=180, key=f"secs_{tup.name if tup else 'cur'}")
        c = st.columns(3)
        mn = c[0].number_input("Minimum words", 1, 100000, int(policy["validation"]["min_words"]))
        mx = c[1].number_input("Maximum words", 2, 200000, int(policy["validation"]["max_words"]))
        fmts = c[2].multiselect("Accepted formats", [".pdf", ".docx", ".pptx"], policy["submission"]["allowed_extensions"])
        if st.button("Save template and format rules", disabled=not tup):
            if run(setup.save_template, conn, user, base_policy, settings.storage_dir, tup.name, tup.getvalue(), setup.sections_from_text(sec_text),
                   int(mn), int(mx), fmts, settings.kb_dir, ok="Template and format rules saved."):
                refresh()
        if not tup and st.button("Save format rules only (keep current template)"):
            if run(setup.update_settings, conn, user, {"validation.required_sections": setup.sections_from_text(sec_text), "validation.min_words": int(mn),
                   "validation.max_words": int(mx), "submission.allowed_extensions": fmts}, base_policy, ok="Format rules saved."):
                setup.publish_template_doc(setup.effective_policy(conn, base_policy), settings.kb_dir)
                refresh()
    with t3:
        with st.form("rules"):
            dl = st.text_input(f"Submission deadline ({policy['timezone']}, format YYYY-MM-DDTHH:MM:SS)", policy["submission"]["deadline"])
            incl = st.checkbox("Accept submissions at exactly the deadline second", policy["submission"].get("deadline_inclusive", True))
            mb = st.number_input("Maximum file size (MB)", 0.1, 100.0, float(policy["submission"]["max_file_mb"]))
            mxe = st.number_input("Problem statements one team may work on (each with its own attempts)", 1, 5,
                                  int(policy["submission"].get("max_problem_statements_per_team", 1)))
            rp = st.text_area("Approved rejection policies, one per line as `ID | title` (empty = reviewers cannot reject)",
                              "\n".join(f"{p['id']} | {p['title']}" for p in policy["review"]["approved_rejection_policies"]))
            if st.form_submit_button("Save rules"):
                pols = [{"id": a.strip(), "title": b.strip()} for a, _, b in (ln.partition("|") for ln in rp.splitlines() if ln.strip())]
                if run(setup.update_settings, conn, user, {"submission.deadline": dl.strip(), "submission.deadline_inclusive": incl,
                       "submission.max_file_mb": mb, "submission.max_problem_statements_per_team": int(mxe),
                       "review.approved_rejection_policies": pols}, base_policy, ok="Rules saved."):
                    refresh()

    with t4:
        v = policy["validation"]
        with st.form("checks"):
            st.markdown("**1. Related to the track (domain)** — keywords come from the track and problem-statement keywords in the catalogue")
            c = st.columns(2)
            rk = c[0].number_input("Minimum keyword matches", 0, 50, int(v["relevance"]["min_keyword_matches"]))
            ra = c[1].selectbox("If not related", ["review", "fail"], ["review", "fail"].index(v["relevance"]["action"]),
                                format_func=lambda x: {"review": "Send to human review", "fail": "Fail the submission"}[x])
            st.markdown("**2. Format** — required headings are set on the *Abstract format / template* tab")
            spw = st.number_input("Minimum words under each heading (0 = only check the heading exists)", 0, 1000, int(v.get("min_words_per_section", 0)))
            st.markdown("**5. Similarity with the problem statements in the database**")
            psc = v.get("ps_similarity", {"enabled": True, "margin": 0.05, "action": "review"})
            c = st.columns(3)
            pe = c[0].checkbox("Enabled", psc.get("enabled", True))
            pm = c[1].number_input("Tolerance (how much closer another PS must be)", 0.0, 1.0, float(psc.get("margin", 0.05)), 0.01)
            pa = c[2].selectbox("If closer to another PS", ["review", "fail"], ["review", "fail"].index(psc.get("action", "review")),
                                format_func=lambda x: {"review": "Send to human review", "fail": "Fail the submission"}[x])
            st.markdown("**6. Duplicate submissions**")
            dc = policy.get("duplicates", {"exact_action": "fail", "near_duplicate_threshold": 0.9, "block_identical_resubmission": True})
            c = st.columns(3)
            da = c[0].selectbox("Exact copy of another team's submission", ["fail", "review"], ["fail", "review"].index(dc["exact_action"]),
                                format_func=lambda x: {"fail": "Reject automatically", "review": "Send to human review"}[x])
            nd = c[1].number_input("Near-duplicate alert at overlap ≥ (0 = off)", 0.0, 1.0, float(dc.get("near_duplicate_threshold") or 0), 0.05)
            bi = c[2].checkbox("Block re-uploading the team's own previous file", dc.get("block_identical_resubmission", True))
            st.caption(f"Similarity above {policy['similarity']['review_threshold']:.0%} with another team's work always goes to human review (never auto-rejected).")
            if st.form_submit_button("Save checks"):
                if run(setup.update_settings, conn, user, {
                        "validation.relevance": {"min_keyword_matches": rk, "action": ra}, "validation.min_words_per_section": spw,
                        "validation.ps_similarity": {"enabled": pe, "margin": pm, "action": pa},
                        "duplicates": {"exact_action": da, "near_duplicate_threshold": nd or None, "block_identical_resubmission": bi}},
                       base_policy, ok="Checks saved."):
                    refresh()
    with t5:
        nc = policy.get("notifications", {})
        st.write("Email:", f"✅ {describe(settings)}" if smtp_configured(settings)
                 else "⚠️ not configured — emails are kept in the outbox. Set the email settings (.env or Space secrets) and restart.")
        with st.form("notif"):
            name = st.text_input("Programme name (email signature)", nc.get("programme_name", "Incubation Programme"))
            url = st.text_input("Portal address included in emails", nc.get("app_url", "http://127.0.0.1:8501"))
            qin = st.text_input("Incubation inbox for student/reviewer queries (also emailed to every admin; optional)", nc.get("query_inbox", ""))
            c = st.columns(3)
            auto = c[0].checkbox("Send immediately after each action", nc.get("auto_send", True))
            mem = c[1].checkbox("Email every team member (not only the contact)", nc.get("notify_members", True))
            rev = c[2].checkbox("Alert reviewers about new review cases", nc.get("alert_reviewers", True))
            hrs = st.text_input("Deadline reminders, hours before the deadline (comma-separated)", ", ".join(str(h) for h in nc.get("reminder_hours_before_deadline", [])))
            rrh = st.number_input("Remind reviewers when a review case waits longer than (hours, 0 = off)", 0, 336, int(nc.get("review_reminder_hours", 24)))
            if st.form_submit_button("Save email settings"):
                try:
                    hl = [int(x) for x in hrs.replace(";", ",").split(",") if x.strip()]
                except ValueError:
                    hl = None
                    st.error("Reminder hours must be whole numbers, e.g. 168, 72, 24, 3")
                if hl is not None and run(setup.update_settings, conn, user, {"notifications": {
                        "programme_name": name, "app_url": url, "auto_send": auto, "notify_members": mem, "alert_reviewers": rev,
                        "reminder_hours_before_deadline": hl, "review_reminder_hours": int(rrh), "query_inbox": qin}}, base_policy, ok="Email settings saved."):
                    refresh()
        st.caption("Reminders go to teams that have not locked a problem statement, not submitted, or must resubmit. "
                   "Run `python scripts/notify_worker.py --loop 300` so reminders and retries happen automatically.")

elif page == "Notifications":
    sv.authorize(conn, user, "admin", action="notifications_page")
    st.header("Emails: acknowledgements, alerts & reminders")
    with st.expander("📣 Send an announcement / update to many teams", expanded=False):
        opts = list(sv.AUDIENCES) + [f"track:{d['id']}" for d in sv.catalog(conn)]
        labels = sv.AUDIENCES | {f"track:{d['id']}": f"Teams in the {d['name']} track" for d in sv.catalog(conn)}
        aud = st.selectbox("Send to", opts, format_func=lambda k: labels[k])
        n_aud = len(sv.teams_for_audience(conn, user, aud))
        st.caption(f"{n_aud} team(s) will receive it (team contact + members, and in the portal).")
        with st.form("announce", clear_on_submit=True):
            cat = st.selectbox("Type", sv.UPDATE_CATEGORIES, index=len(sv.UPDATE_CATEGORIES) - 1)
            subj = st.text_input("Subject", placeholder="e.g. Shortlist for Phase 2 published")
            msg = st.text_area("Message", height=160)
            sure = st.checkbox("I have checked the audience and the text")
            if st.form_submit_button("Send announcement", disabled=n_aud == 0):
                if not sure:
                    st.warning("Tick the confirmation box first.")
                elif n := run(sv.send_announcement, conn, policy, user, aud, cat, subj, msg):
                    st.success(f"Announcement sent to {n} team(s).")
    cnt = outbox_counts(conn)
    c = st.columns(4)
    c[0].metric("Queued", cnt.get("queued_local", 0))
    c[1].metric("Sent", cnt.get("sent", 0))
    c[2].metric("Failed", cnt.get("failed", 0))
    c[3].metric("Email", ("Brevo" if settings.email_provider == "brevo" else "SMTP") if smtp_configured(settings) else "not set")
    c = st.columns(3)
    if c[0].button("Send pending emails now"):
        st.write(deliver_pending(conn, settings))
    waiting = count_waiting(conn)
    if waiting:
        with st.expander(f"🗑️ Discard the {waiting} email(s) still waiting to be sent"):
            st.caption("Use this once, before or right after setting up email, if the waiting emails are old test messages you don't want "
                       "to go out. They stay listed below, marked as discarded. New emails are not affected.")
            ok_discard = st.checkbox("Yes, never send these waiting emails")
            if st.button("Discard waiting emails", disabled=not ok_discard):
                n = run(sv.discard_waiting_emails, conn, user)
                if n:
                    st.success(f"{n} email(s) discarded.")
                    refresh()
    if c[1].button("Queue due reminders now"):
        n = send_deadline_reminders(conn, policy, sv.deadline(policy), sv.now_utc())
        r = send_review_reminders(conn, policy, sv.now_utc())
        sv.audit(conn, user, "reminders_queued", details={"deadline": n, "review": r})
        st.success(f"{n} deadline reminder(s) and {r} reviewer reminder(s) queued" + ("" if n or r else " (none due right now)"))
    with c[2].form("test_mail", border=False):
        to = st.text_input("Send a test email to")
        if st.form_submit_button("Send test email") and to:
            okm, msg = send_test_email(settings, to.strip())
            (st.success if okm else st.error)(msg)
    fails = recent_failures(conn)
    if fails:
        st.markdown("**Recent failures**")
        st.dataframe(pd.DataFrame(fails), hide_index=True, width="stretch")
    flt = st.selectbox("Show", ["all", "queued_local", "sent", "failed"])
    q = "SELECT id, recipient, event_type, subject, status, attempts, last_error, created_at, sent_at FROM notifications"
    rows = conn.execute(q + ("" if flt == "all" else " WHERE status=?") + " ORDER BY id DESC LIMIT 500", (() if flt == "all" else (flt,))).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    st.dataframe(df, hide_index=True, width="stretch")
    if not df.empty:
        nid = st.selectbox("Preview message", df["id"].tolist())
        r = conn.execute("SELECT subject, body FROM notifications WHERE id=?", (int(nid),)).fetchone()
        st.text(f"Subject: {r['subject']}\n\n{r['body']}")

elif page == "Audit Log":
    st.header("Audit log (append-only)")
    a = st.text_input("Filter action")
    st.dataframe(pd.DataFrame(sv.audit_log(conn, user, 1000, a)), hide_index=True, width="stretch")

elif page == "Policy":
    sv.authorize(conn, user, "admin", action="policy_page")
    st.header("Active policy")
    ps = persist.status()
    if ps["enabled"]:
        c1, c2 = st.columns([3, 1])
        c1.info(f"💾 {ps['message']}")
        if c2.button("Back up now"):
            r = cloud.push(force=True) if cloud.enabled() else persist.backup()
            st.success(f"Saved: database {'copied' if r.get('db_copied') else 'unchanged'}, {r.get('files_copied', 0)} file(s) copied.")
    st.caption(f"Edit `{settings.policy_file}` and restart to change rules. Items awaiting administrator confirmation:")
    st.write(policy.get("awaiting_admin_confirmation", []))
    if st.button("Run deadline sweep now"):
        st.write(f"Marked {run(sv.sweep_deadlines, conn, policy, user)} submission(s) DEADLINE_MISSED.")
    st.json({k: v for k, v in policy.items() if not k.startswith("_")})

elif page == "Queries":
    st.header("📬 Queries from students and reviewers")
    cnt = qs.counts(conn)
    c = st.columns(4)
    c[0].metric("Waiting for reply", cnt.get("open", 0))
    c[1].metric("Answered", cnt.get("answered", 0))
    c[2].metric("Closed", cnt.get("closed", 0))
    c[3].metric("Inbox copy to", policy.get("notifications", {}).get("query_inbox") or "admins only")
    f = st.columns(3)
    fst = f[0].selectbox("Status", ["open", "answered", "closed", ""], format_func=lambda x: {"": "All", "open": "Waiting for reply"}.get(x, x.title()))
    fcat = f[1].selectbox("Topic", [""] + qs.CATEGORIES, format_func=lambda x: x or "All topics")
    fq = f[2].text_input("Search (subject, user, team, text)")
    rows = qs.inbox(conn, user, fst, fcat, fq)
    if not rows:
        st.info("No queries match.")
        st.stop()
    st.dataframe(pd.DataFrame([{"Ref": r["ref"], "Status": r["status"], "Topic": r["category"], "Subject": r["subject"], "From": r["username"],
                                "Role": r["role"], "Team": r["team_code"] or "", "Messages": r["n_messages"], "Updated": r["updated_at"][:16].replace("T", " ")}
                               for r in rows]), hide_index=True, width="stretch")
    qid = st.selectbox("Open a query", [r["id"] for r in rows], format_func=lambda i: next(f"{r['ref']} · {r['subject']} · {r['username']}" for r in rows if r["id"] == i))
    q = next(r for r in rows if r["id"] == qid)
    with st.container(border=True):
        st.markdown(f"**{q['ref']} · {q['subject']}**  \n{q['category']} · from **{q['username']}** ({q['role']}{', team ' + q['team_code'] if q['team_code'] else ''})"
                    f" · {q['email'] or 'no email on file'}")
        for m in qs.thread(conn, user, qid):
            st.markdown(f"**{'🏢 ' + (m['author'] or 'staff') if m['from_staff'] else '🙋 ' + (m['author'] or '')}** · {m['at'][:16].replace('T', ' ')} UTC")
            st.write(m["body"])
    key = f"qreply{qid}"
    if st.button("✨ Draft a reply with AI (from the knowledge base)", disabled=not model_status()["chat_ok"]):
        idx = load_index()
        if idx is None:
            st.warning("Rebuild the knowledge-base index first (📚 Knowledge Base).")
        else:
            with st.spinner("Drafting…"):
                d = run(qs.ai_draft, conn, user, qid, idx, client, policy["rag"])
            if d:
                st.session_state[key] = d
    if not model_status()["chat_ok"]:
        st.caption("AI drafting needs an AI key: ⚙️ Programme Setup → AI keys.")
    txt = st.text_area("Your reply (emailed to the sender)", key=key, height=200)
    b = st.columns(4)
    if b[0].button("Send reply", type="primary") and run(qs.reply, conn, policy, user, qid, txt, ok="Reply sent."):
        st.session_state.pop(key, None)
        refresh()
    if b[1].button("Send and close") and run(qs.reply, conn, policy, user, qid, txt, True, ok="Reply sent and query closed."):
        st.session_state.pop(key, None)
        refresh()
    if q["status"] != "closed" and b[2].button("Close without reply") and run(qs.set_status, conn, user, qid, "closed", ok="Closed."):
        refresh()
    if q["status"] == "closed" and b[3].button("Reopen") and run(qs.set_status, conn, user, qid, "open", ok="Reopened."):
        refresh()

elif page == "AI Assistant":
    st.header("🤖 AI assistant (staff only)")
    st.caption("Ask questions about the programme rules; answers come from the approved knowledge base with citations.")
    idx = load_index()
    if idx is None:
        st.warning("Knowledge-base index is missing or stale. An administrator must run `python scripts/ingest.py`.")
        st.stop()
    ms = model_status()
    if not ms["chat_ok"]:
        st.info("Local chat model unavailable: answers will show the most relevant approved passages only. " + "; ".join(ms["instructions"]))
    for msg in st.session_state.setdefault("chat", []):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
    if q := st.chat_input("Ask about the incubation rules…"):
        with st.chat_message("user"):
            st.markdown(q)
        res = answer(q, idx, user.role, client if ms["chat_ok"] else None, policy["rag"], conn)
        body = res["answer"]
        if res["status"] == "uncited":
            body = "⚠️ The model's answer could not be verified against cited sources:\n\n" + body
        body += "".join(f"\n\n> **[{s['tag']}] {s['label']}**  \n> {s['text'][:400]}…" for s in res["sources"])
        body += f"\n\n_retrieval: {res['mode']} · {res['latency_ms']} ms · status: {res['status']}_"
        with st.chat_message("assistant"):
            st.markdown(body)
        st.session_state["chat"] += [{"role": "user", "content": q}, {"role": "assistant", "content": body}]
