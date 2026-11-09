"""Phase 1 Incubation RAG Agent - Streamlit UI. Run: streamlit run app.py"""
from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

import cloud
from evaluation.metrics import workflow_summary
from notifications.outbox import count_waiting, deliver_pending, describe, send_test_email, smtp_configured
from persist import snapshot as persist
from notifications.service import outbox_counts, recent_failures, send_deadline_reminders, send_review_reminders
from rag.answer import answer
from rag.index import HybridIndex
from rag.ollama import make_client
from security.auth import AuthError
from workflow import evaluation as ev
from workflow import services as sv
from workflow import setup
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
client = make_client(settings)


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


def run(fn, *a, ok: str | None = None, **kw):
    """Call a backend service and surface its real outcome."""
    try:
        res = fn(*a, **kw)
        if ok:
            st.success(ok)
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
            st.rerun()
    ms = model_status()
    st.caption((f"🟢 AI: {client.chat_model} (cloud)" if ms.get("provider") == "api" else "🟢 Ollama reachable" if ms["reachable"] else "🔴 Ollama unreachable — AI features disabled, workflow still works")
               + ("" if not ms["reachable"] else f" · chat {'✓' if ms['chat_ok'] else '✗'} · embed {'✓' if ms['embed_ok'] else '✗'}"))

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
                    st.rerun()
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
        "Dashboard": "📊", "Programme Setup": "⚙️", "My Reviews": "📝", "Peer Review": "🎯", "Results": "🏅", "Users": "🔑", "Knowledge Base": "📚", "Notifications": "✉️", "Audit Log": "🧾", "Policy": "📜"}


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


def progress_tracker(team_id):
    """Student journey: account -> team -> problem statement -> submission -> result."""
    t = conn.execute("SELECT ps_locked FROM teams WHERE id=?", (team_id,)).fetchone() if team_id else None
    last = sv.latest_submission(conn, team_id) if team_id else None
    status = last["status"] if last else None
    result = {"ACCEPTED_FOR_EVALUATION": "✅ Accepted", "PENDING_REVIEW": "🧑‍⚖️ Under review", "REVIEW_CLEARED": "🧑‍⚖️ Under review",
              "RESUBMISSION_ALLOWED": "✏️ Fix & resubmit", "DROPPED": "⛔ Closed", "DEADLINE_MISSED": "⛔ Closed",
              "REJECTED_BY_AUTHORIZED_REVIEW": "⛔ Not accepted"}.get(status, "Result")
    res = ev.team_result(conn, policy, user) if user.role == "student" and team_id else None
    if res:
        result = {"shortlisted": "🏅 Shortlisted", "waitlisted": "⏳ Waitlisted", "not_shortlisted": "Not shortlisted"}[res["outcome"]]
    elif status == "ACCEPTED_FOR_EVALUATION":
        result = "✅ Accepted · being evaluated"
    done = [True, bool(team_id), bool(t and t["ps_locked"]), bool(last), status == "ACCEPTED_FOR_EVALUATION"]
    labels = ["Account", "Team registered", "Problem statement locked", "Abstract submitted", result]
    now = done.index(False) if False in done else None
    cols = st.columns(5)
    for i, (c, lab) in enumerate(zip(cols, labels)):
        cls = "done" if done[i] else ("now" if i == now else "")
        c.markdown(f"<div class='step {cls}'>{'✔' if done[i] else i + 1} · {lab}</div>", unsafe_allow_html=True)


PAGES = {
    "student": ["My Team", "Submit", "History", "Results", "Policy Assistant"],
    "reviewer": ["My Reviews", "Review Queue", "Policy Assistant"],
    "admin": ["Dashboard", "Programme Setup", "Review Queue", "Submissions", "Peer Review", "Users", "Knowledge Base", "Notifications", "Audit Log", "Policy"],
}
page = st.sidebar.radio("Navigate", PAGES[user.role], format_func=lambda p: f"{ICON.get(p, '•')} {p}")
hero()

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
                st.rerun()
    else:
        t = sv.get_team(conn, user, user.team_id)
        st.subheader(f"{t['name']}  ·  {t['team_code']}  🔒")
        st.dataframe(pd.DataFrame(t["members"]), hide_index=True)
        if t["ps_locked"]:
            ps = conn.execute("SELECT * FROM problem_statements WHERE id=?", (t["ps_id"],)).fetchone()
            st.success(f"🔒 Locked: {t['domain_id']} / {ps['id']} — {ps['title']}")
            ps_card(dict(ps))
        else:
            cat = sv.catalog(conn)
            if not cat:
                st.warning("No tracks are open yet. Please check back after the administrator uploads them.")
                st.stop()
            st.markdown("#### Choose your track and problem statement")
            d = st.selectbox("Track (domain)", cat, format_func=lambda x: f"{x['id']} — {x['name']} ({len(x['problem_statements'])} problem statements)")
            ps = st.selectbox("Problem statement", d["problem_statements"], format_func=lambda x: f"{x['id']} — {x['title']}")
            ps_card(ps, d["name"])
            with st.expander(f"📚 Browse all {sum(len(x['problem_statements']) for x in cat)} problem statements"):
                qq = st.text_input("Search title or description").lower()
                for dd in cat:
                    for p_ in dd["problem_statements"]:
                        if not qq or qq in (p_["title"] + " " + (p_["description"] or "")).lower():
                            ps_card(p_, dd["name"])
            ok = st.checkbox("I understand this selection cannot be changed later.")
            if st.button("Confirm and lock", disabled=not ok) and run(sv.select_problem_statement, conn, user, d["id"], ps["id"], policy=policy, ok="Selection locked."):
                st.rerun()

elif page == "Submit":
    st.header("Submit abstract")
    if not user.team_id:
        st.warning("Register a team first.")
        st.stop()
    progress_tracker(user.team_id)
    okg, code, msg, used = sv.submission_gate(conn, policy, user.team_id, sv.now_utc())
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
                dr = run(sv.check_draft, conn, policy, user, dfile.name, dfile.getvalue(), embedder=client if model_status()["embed_ok"] else None)
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
            rep = run(sv.submit, conn, policy, settings.storage_dir, user, f.name, f.getvalue(), embedder=client if ms["embed_ok"] else None)
        if rep:  # rerun so the attempt counters above reflect this submission
            st.session_state["last_report"] = rep
            st.session_state["uploader_gen"] = st.session_state.get("uploader_gen", 0) + 1
            st.rerun()
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
        with st.expander(f"Attempt {r['attempt_no']} · {r['file_name']} · {r['status']}"):
            show_report(sv.report(conn, user, r["id"]))

elif page == "Results":
    st.header("Evaluation result")
    res = ev.team_result(conn, policy, user)
    if not res:
        last = sv.latest_submission(conn, user.team_id) if user.team_id else None
        if last and last["status"] == "ACCEPTED_FOR_EVALUATION":
            st.info("✅ Your abstract was accepted for evaluation and is with the reviewers. You will get an email when results are released.")
        else:
            st.info("Results appear here after your abstract has been evaluated and the organisers release the results.")
    else:
        (st.success if res["outcome"] == "shortlisted" else st.warning if res["outcome"] == "waitlisted" else st.error)(f"**{res['label']}** · abstract {res['paper']}")
        if res["mean_score"] is not None:
            st.metric("Average reviewer score", f"{res['mean_score']} / 100")
        if res["note"]:
            st.markdown("**Message from the organisers**")
            st.write(res["note"])
        for i, c in enumerate(res["comments"], 1):
            with st.container(border=True):
                st.markdown(f"**Reviewer {i}**")
                st.write(c)

# ---------------- reviewer / admin ----------------
elif page == "My Reviews":
    st.header("📝 My review assignments")
    ec = policy["evaluation"]
    if not ev.has_pledged(conn, user.id):
        with st.container(border=True):
            st.markdown("#### 🔒 Reviewer confidentiality pledge")
            st.write(ev.PLEDGE)
            if st.checkbox("I agree", key="pledge_ok") and st.button("Confirm pledge", type="primary") and run(ev.pledge, conn, user, ok="Thank you."):
                st.rerun()
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
                st.rerun()
            with b[1].popover("Decline / conflict of interest"):
                why = st.text_area("Reason", key=f"why{aid}")
                coi = st.checkbox("I have a conflict of interest with this team", key=f"coi{aid}")
                if st.button("Decline", key=f"dec{aid}") and run(ev.respond, conn, policy, user, aid, False, why, coi, ok="Declined. The organisers were told."):
                    st.rerun()
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
                st.rerun()
        others = ev.peer_reviews_visible_to(conn, policy, user, aid)
        if len(others) > 1:
            st.markdown("#### 💬 Other reviews of this abstract (visible after you submit yours)")
            st.dataframe(pd.DataFrame(others), hide_index=True, width="stretch")
    with st.expander("Declare a conflict of interest with this team later"):
        why2 = st.text_input("Reason", key=f"coi2{aid}")
        if st.button("Declare conflict and withdraw from this abstract", key=f"coib{aid}"):
            tid = conn.execute("SELECT team_id FROM submissions WHERE id=?", (r["submission_id"],)).fetchone()[0]
            if run(ev.declare_conflict, conn, user, user.id, tid, why2, ok="Conflict recorded; the assignment was withdrawn."):
                st.rerun()

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
                    st.rerun()
            st.markdown("**Conflicts of interest** (a conflicted reviewer is never assigned to that team; the same email as a team member is detected automatically)")
            teams = [dict(x) for x in conn.execute("SELECT id, team_code, name FROM teams ORDER BY team_code")]
            if teams:
                with st.form("coi"):
                    cc = st.columns(3)
                    crv = cc[0].selectbox("Reviewer", [p["id"] for p in pool], format_func=lambda i: next(p["username"] for p in pool if p["id"] == i), key="coir")
                    ctm = cc[1].selectbox("Team", [x["id"] for x in teams], format_func=lambda i: next(f"{x['team_code']} · {x['name']}" for x in teams if x["id"] == i))
                    cwhy = cc[2].text_input("Reason")
                    if st.form_submit_button("Record conflict") and run(ev.declare_conflict, conn, user, crv, ctm, cwhy, ok="Conflict recorded."):
                        st.rerun()
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
            st.markdown("**Assign manually**")
            with st.form("assign"):
                sid = st.selectbox("Abstract", [x["submission_id"] for x in subs],
                                   format_func=lambda i: next(f"{x['paper']} · {x['team_code']} · {x['track']} · {x['assigned']} assigned" for x in subs if x["submission_id"] == i))
                srow = next(x for x in subs if x["submission_id"] == sid)
                tid = conn.execute("SELECT team_id FROM submissions WHERE id=?", (sid,)).fetchone()[0]
                opts = [p["id"] for p in pool if p["active"]]
                lab = {p["id"]: f"{p['username']} · load {p['load']}/{p['max_load']}" + (" · ⭐ track expert" if srow["domain_id"] in p["expertise"] else "")
                       + (" · ⛔ conflict" if ev.conflict_reason(conn, p["id"], tid) else "") for p in pool}
                picks = st.multiselect("Reviewers", opts, format_func=lambda i: lab[i])
                due = st.date_input("Review due date", sv.now_utc().date() + pd.Timedelta(days=ec["review_days"]))
                if st.form_submit_button("Assign and email reviewers"):
                    okn = sum(bool(run(ev.assign, conn, policy, user, sid, rid, due)) for rid in picks)
                    if okn:
                        st.success(f"{okn} reviewer(s) assigned and emailed.")
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
                        st.rerun()
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
                    st.rerun()
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
                        st.rerun()

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
                 "Attach a Storage Bucket at /data (DEPLOYMENT.md, step 'Keep your data').")
    m = workflow_summary(conn)
    c = st.columns(5)
    c[0].metric("Teams", m["teams"])
    c[1].metric("PS locked", m["teams_ps_locked"])
    c[2].metric("Review backlog", m["review_backlog"])
    c[3].metric("Avg processing (ms)", m["avg_processing_ms"] or "-")
    c[4].metric("Deadline failures", m["deadline_missed"])
    evs = ev.evaluable(conn, user, policy)
    if evs:
        c = st.columns(4)
        c[0].metric("Abstracts in peer review", len(evs))
        c[1].metric("Waiting for reviewers", sum(x["needed"] > 0 for x in evs))
        c[2].metric("Reviews submitted", sum(x["reviews_done"] for x in evs))
        c[3].metric("Shortlisted", sum(x["decision"] == "shortlisted" for x in evs))
    per_track = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT COALESCE(d.name, 'Not chosen yet') AS track, COUNT(*) AS teams FROM teams t LEFT JOIN domains d ON d.id=t.domain_id GROUP BY 1 ORDER BY 2 DESC")])
    if not per_track.empty:
        st.markdown("**Teams per track**")
        st.bar_chart(per_track.set_index("track"))
    for title, key in [("Submissions by status", "submissions_by_status"), ("Attempt distribution", "attempt_distribution"),
                       ("Failed checks", "failed_checks"), ("Blocked submissions (no attempt consumed)", "blocked_submissions"),
                       ("Notifications", "notifications"), ("RAG queries by status", "rag_status")]:
        st.markdown(f"**{title}**")
        if m[key]:
            st.bar_chart(pd.Series(m[key]).rename(index=str))
        else:
            st.caption("No data yet.")
    st.caption(f"RAG avg latency: {m['rag_avg_latency_ms'] or '-'} ms over {m['rag_queries']} queries · state-transition errors: {m['state_transition_errors']}")
    ev = conn.execute("SELECT name, value, labels, at FROM metrics WHERE name LIKE 'eval_%' ORDER BY id DESC LIMIT 20").fetchall()
    if ev:
        st.markdown("**Latest evaluation runs** (from `python scripts/evaluate.py`)")
        st.dataframe(pd.DataFrame([dict(r) for r in ev]), hide_index=True)

elif page == "Users":
    sv.authorize(conn, user, "admin", action="users_page")
    st.header("Users")
    with st.form("nu"):
        un, pw = st.text_input("Username"), st.text_input("Temporary password", type="password")
        role = st.selectbox("Role", ["reviewer", "admin", "student"])
        em = st.text_input("Email (reviewers/admins receive review alerts here)")
        if st.form_submit_button("Create user"):
            run(sv.admin_create_user, conn, user, un, pw, role, em, policy, ok="User created.")
    users_df = pd.DataFrame([dict(r) for r in conn.execute("SELECT id, username, role, email, team_id, active, created_at FROM users")])
    st.dataframe(users_df, hide_index=True)
    with st.form("set_email"):
        c = st.columns([2, 3, 1])
        uid = c[0].selectbox("User", users_df["id"].tolist(), format_func=lambda i: users_df.set_index("id").loc[i, "username"])
        new_em = c[1].text_input("New email (empty = remove)")
        c[2].write("")
        if c[2].form_submit_button("Save email"):
            if run(sv.set_user_email, conn, user, int(uid), new_em, policy, ok="Email saved."):
                pass
            st.rerun()

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
            st.rerun()
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
    t1, t2, t3, t4, t5, t6 = st.tabs(["Tracks & problem statements", "Abstract format / template", "Deadline & rules", "Validation checks", "Emails & reminders", "Peer review rubric"])
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
                        st.rerun()
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
                st.rerun()
        if not tup and st.button("Save format rules only (keep current template)"):
            if run(setup.update_settings, conn, user, {"validation.required_sections": setup.sections_from_text(sec_text), "validation.min_words": int(mn),
                   "validation.max_words": int(mx), "submission.allowed_extensions": fmts}, base_policy, ok="Format rules saved."):
                setup.publish_template_doc(setup.effective_policy(conn, base_policy), settings.kb_dir)
                st.rerun()
    with t3:
        with st.form("rules"):
            dl = st.text_input(f"Submission deadline ({policy['timezone']}, format YYYY-MM-DDTHH:MM:SS)", policy["submission"]["deadline"])
            incl = st.checkbox("Accept submissions at exactly the deadline second", policy["submission"].get("deadline_inclusive", True))
            mb = st.number_input("Maximum file size (MB)", 0.1, 100.0, float(policy["submission"]["max_file_mb"]))
            rp = st.text_area("Approved rejection policies, one per line as `ID | title` (empty = reviewers cannot reject)",
                              "\n".join(f"{p['id']} | {p['title']}" for p in policy["review"]["approved_rejection_policies"]))
            if st.form_submit_button("Save rules"):
                pols = [{"id": a.strip(), "title": b.strip()} for a, _, b in (ln.partition("|") for ln in rp.splitlines() if ln.strip())]
                if run(setup.update_settings, conn, user, {"submission.deadline": dl.strip(), "submission.deadline_inclusive": incl,
                       "submission.max_file_mb": mb, "review.approved_rejection_policies": pols}, base_policy, ok="Rules saved."):
                    st.rerun()

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
                    st.rerun()
    with t5:
        nc = policy.get("notifications", {})
        st.write("Email:", f"✅ {describe(settings)}" if smtp_configured(settings)
                 else "⚠️ not configured — emails are kept in the outbox. Set the email settings (.env or Space secrets) and restart.")
        with st.form("notif"):
            name = st.text_input("Programme name (email signature)", nc.get("programme_name", "Incubation Programme"))
            url = st.text_input("Portal address included in emails", nc.get("app_url", "http://127.0.0.1:8501"))
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
                        "reminder_hours_before_deadline": hl, "review_reminder_hours": int(rrh)}}, base_policy, ok="Email settings saved."):
                    st.rerun()
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
                    st.rerun()
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

elif page == "Policy Assistant":
    st.header("Policy assistant")
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
