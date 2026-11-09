"""Reviewers/admins can open the original uploaded abstract; students only their own team's."""
import shutil
from pathlib import Path

import pytest

from conftest import do_submit, good_docx, make_team
from security.auth import AuthError
from workflow import services as sv


def test_reviewer_admin_and_owner_can_download(env):
    sv.admin_create_user(env["conn"], env["admin"], "rev_u", "reviewpass1", "reviewer", "rev@example.org")
    rev = sv.authenticate(env["conn"], "rev_u", "reviewpass1")
    a, b = make_team(env, "a"), make_team(env, "b")
    data = good_docx(1)
    sid = do_submit(env, a, data)["submission_id"]
    with pytest.raises(AuthError):  # reviewers only see abstracts assigned to them
        sv.submission_file(env["conn"], rev, sid)
    from workflow import evaluation as ev
    ev.assign(env["conn"], env["p"], env["admin"], sid, rev.id, now=sv.now_utc())
    for who in (rev, env["admin"], a):
        name, got = sv.submission_file(env["conn"], who, sid)
        assert got == data and name.endswith(".docx")
    with pytest.raises(AuthError):
        sv.submission_file(env["conn"], b, sid)
    sv.log_file_download(env["conn"], rev, sid, name)
    assert env["conn"].execute("SELECT COUNT(*) FROM audit_events WHERE action='submission_file_downloaded'").fetchone()[0] == 1


def test_found_after_storage_folder_moved(env, tmp_path):
    a = make_team(env, "a")
    sid = do_submit(env, a, good_docx(2))["submission_id"]
    old = Path(env["conn"].execute("SELECT stored_path FROM submissions WHERE id=?", (sid,)).fetchone()[0])
    new_root = tmp_path / "moved_storage"
    shutil.copytree(old.parent.parent.parent, new_root)
    shutil.rmtree(old.parent)
    with pytest.raises(sv.WorkflowError):
        sv.submission_file(env["conn"], env["admin"], sid)
    assert sv.submission_file(env["conn"], env["admin"], sid, new_root)[1] == good_docx(2)
