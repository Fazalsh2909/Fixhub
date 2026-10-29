"""Deterministic gate suite for the manifest-based workspace sync (#70/#71).

Production incident: tasks #70/#71 published a PR whose `.github/workflows/
ci.yml` edit never reached the host workspace — the old sync loop silently
skipped every failure and a later `git add` staged everything except the
fix. This suite pins the replacement contract without a real VM or GitHub
credentials:

- the guest side is exercised directly against a local guest tree
  (`app.sandbox.guest.agent._handle` — the code baked into the rootfs);
- the host side runs the REAL `sync_guest_to_host` against a fake `_VM`
  whose vsock client is a local-protocol fake (manifest + raw read ops).

Scenario classes (one test each, per the incident fix plan):
  1 hidden-dir edit reaches host git status/diff
  2 sync errors surface with path+stage (never skipped)
  3 digest disagreement fails closed, nothing written
  4 legacy rootfs (no manifest op) demands a rebuild — no silent fallback
  5 mode preservation + unchanged files untouched (mtime evidence)
  6 add/change/delete round trip incl. hidden dirs + verified state
  7 truncated manifest fails closed and never drives deletions
  8 hard sync failure blocks publish (task FAILED, remote untouched)
  9 declared-edit missing -> NEEDS_REVIEW with workspace preserved

`_publish_task` stays untouched by this suite except as a "never called"
guard; lease/security suites own that boundary.
"""

import base64
import hashlib
import os
import stat
import subprocess
from contextlib import contextmanager

import pytest

from app.sandbox import firecracker as _fc
from app.sandbox import manifest as _manifest

TASK_ID = 90271

_CI_YML_OLD = "name: CI\non: push\njobs: {}\n"
_CI_YML_FIXED = (
    "name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
)


def _git(path, *args):
    r = subprocess.run(
        ["git", *args], cwd=str(path), capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# --------------------------------------------------------------------------
# Guest-side direct tests (the code baked into the rootfs image)
# --------------------------------------------------------------------------


def _guest_tree(tmp_path):
    root = tmp_path / "guest"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "api").mkdir()
    (root / ".git" / "objects").mkdir(parents=True)
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(_CI_YML_FIXED, newline="\n")
    (root / "api" / "app.py").write_text("x = 1\n", newline="\n")
    (root / ".git" / "config").write_text("secret-remote\n")
    (root / "pkg" / "__pycache__" / "c.pyc").write_bytes(b"\x00\x01")
    return root


def test_guest_manifest_lists_hidden_dirs_with_correct_digests(tmp_path):
    from app.sandbox.guest import agent as _gagent

    root = _guest_tree(tmp_path)
    resp = _gagent._handle({"op": "manifest", "path": "."}, str(root))
    assert "error" not in resp
    assert resp["truncated"] is False
    by_path = {e["path"]: e for e in resp["entries"]}
    ci = by_path[".github/workflows/ci.yml"]  # hidden dirs INCLUDED
    assert ci["size"] == len(_CI_YML_FIXED.encode())
    assert ci["sha256"] == _sha(_CI_YML_FIXED.encode())
    assert by_path["api/app.py"]["sha256"] == _sha(b"x = 1\n")
    # exclusions: .git internals and __pycache__ never ship
    assert ".git/config" not in by_path
    assert not any("__pycache__" in p for p in by_path)


def test_guest_manifest_truncation_is_explicit(tmp_path):
    from app.sandbox.guest import agent as _gagent

    root = _guest_tree(tmp_path)
    resp = _gagent._handle(
        {"op": "manifest", "path": ".", "max_entries": 1}, str(root)
    )
    assert resp["truncated"] is True
    assert len(resp["entries"]) == 1


def test_guest_manifest_max_entries_clamped_to_frame_bound(tmp_path):
    from app.sandbox.guest import agent as _gagent

    root = _guest_tree(tmp_path)
    resp = _gagent._handle(
        {"op": "manifest", "path": ".", "max_entries": 10**9}, str(root)
    )
    # tiny tree stays untruncated under the giant clamp request
    assert resp["truncated"] is False
    assert len(resp["entries"]) == 2


def test_guest_raw_read_is_byte_exact_and_refuses_caps(tmp_path):
    from app.sandbox.guest import agent as _gagent

    root = _guest_tree(tmp_path)
    raw = b"\x00\x01binary\xff\xfe\x00"
    (root / "blob.bin").write_bytes(raw)
    resp = _gagent._handle(
        {"op": "read", "path": "blob.bin", "mode": "raw"}, str(root)
    )
    assert base64.b64decode(resp["content_b64"]) == raw
    assert resp["size"] == len(raw)
    # text path unchanged (legacy consumers keep their contract)
    txt = _gagent._handle({"op": "read", "path": "api/app.py"}, str(root))
    assert txt["content"] == "x = 1\n"
    # over-cap file REFUSED, never truncated
    (root / "big.bin").write_bytes(b"z" * 2_000_001)
    resp = _gagent._handle(
        {"op": "read", "path": "big.bin", "mode": "raw"}, str(root)
    )
    assert "exceeds raw read cap" in resp["error"]


def test_guest_raw_read_blocks_sensitive_and_traversal(tmp_path):
    from app.sandbox.guest import agent as _gagent

    root = _guest_tree(tmp_path)
    resp = _gagent._handle({"op": "read", "path": ".env", "mode": "raw"}, str(root))
    assert "blocked" in resp["error"]
    resp = _gagent._handle(
        {"op": "read", "path": "../../etc/passwd", "mode": "raw"}, str(root)
    )
    assert "error" in resp


# --------------------------------------------------------------------------
# manifest.py pure-engine tests
# --------------------------------------------------------------------------


def test_manifest_engine_rejects_malformed_entries():
    entries, truncated = _manifest.parse_manifest(
        {
            "entries": [{"path": "a/b.txt", "size": 3, "sha256": "a" * 64}],
            "truncated": False,
        }
    )
    assert truncated is False
    assert entries[0].path == "a/b.txt"
    bad_cases = (
        ({"path": "../escape", "size": 1, "sha256": "a" * 64}, "escape"),
        ({"path": ".env", "size": 1, "sha256": "a" * 64}, "sensitive"),
        ({"path": "x", "size": -1, "sha256": "a" * 64}, "size"),
        ({"path": "x", "size": 1, "sha256": "zz" * 32}, "digest"),
        ({}, "path"),
    )
    for bad, why in bad_cases:
        with pytest.raises(_manifest.ManifestInvalid) as ei:
            _manifest.parse_manifest({"entries": [bad], "truncated": False})
        assert why in str(ei.value)


def test_sync_state_is_sibling_of_workspace_and_survives_corruption(tmp_path):
    ws = tmp_path / "task-99"
    ws.mkdir()
    state_path = _manifest.sync_state_path(str(ws), 99)
    _manifest.save_sync_state(
        state_path, [_manifest.ManifestEntry("a.txt", 1, "a" * 64)]
    )
    parent = os.path.dirname(os.path.abspath(str(ws)))
    snap = os.path.join(parent, ".fixhub-sync")
    assert os.path.isfile(os.path.join(snap, "task-99.json"))
    # the state dir is a SIBLING: nothing state-shaped inside the git tree
    assert not os.path.exists(os.path.join(str(ws), ".fixhub-sync"))
    # snapshot round trips
    loaded = _manifest.load_sync_state(state_path)
    assert set(loaded) == {"a.txt"}
    assert loaded["a.txt"].sha256 == "a" * 64
    # corrupt/missing snapshots never break a later deletion computation
    with open(state_path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert _manifest.load_sync_state(state_path) == {}


# --------------------------------------------------------------------------
# Host-side sync against a fake VM speaking the real protocol
# --------------------------------------------------------------------------


class FakeVM:
    def __init__(self, chroot):
        self.task_id = TASK_ID
        self.jail_id = f"task-{TASK_ID}"
        self.chroot_dir = str(chroot)
        self.api_socket = ""
        self.cid = _fc._cid_for(TASK_ID)
        self.overlay = ""
        self.ready = True


class FakeVsockClient:
    """Local protocol fake for `_guest.VsockClient`.

    Speaks manifest + raw-read exactly as the REAL guest agent does
    (hidden dirs included, .git/__pycache__ excluded, explicit truncation,
    byte-exact base64 reads). `flaws` injects failures:
      unknown_op(bool)  manifest_error(str)  read_error(rel-path str)
      digest_mismatch(rel-path str)
    """

    def __init__(self, state, *args, **kwargs):
        self.guest = state["guest_root"]
        self.flaws = state["flaws"]

    def call(self, req, timeout_s=30):  # same call signature as the real one
        req_id = req.get("req_id", "")
        op = req.get("op")
        if op == "manifest":
            if self.flaws.get("unknown_op"):
                return {
                    "req_id": req_id,
                    "error": "unknown op: manifest",
                    "exit_code": 1,
                }
            if self.flaws.get("manifest_error"):
                return {"req_id": req_id, "error": self.flaws["manifest_error"]}
            max_entries = int(req.get("max_entries", 8000) or 8000)
            entries = []
            truncated = False
            root = self.guest
            for r, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
                for name in sorted(dirs + files):
                    fp = os.path.join(r, name)
                    rel = os.path.relpath(fp, root).replace(os.sep, "/")
                    if (
                        "__pycache__" in rel
                        or rel == ".git"
                        or rel.startswith(".git/")
                    ):
                        continue  # `.git` internals only — never `.github/*`
                    if not stat.S_ISREG(os.stat(fp).st_mode):
                        continue
                    if len(entries) >= max_entries:
                        truncated = True
                        break
                    with open(fp, "rb") as fh:
                        data = fh.read()
                    dig = _sha(data)
                    if self.flaws.get("digest_mismatch") == rel:
                        dig = "b" * 64  # manifest lies about this file
                    entries.append({"path": rel, "size": len(data), "sha256": dig})
                if truncated:
                    break
            return {
                "req_id": req_id,
                "entries": entries,
                "truncated": truncated,
            }
        if op == "read":
            rel = str(req.get("path", ""))
            if self.flaws.get("read_error") == rel:
                return {
                    "req_id": req_id,
                    "error": "simulated guest read refusal",
                }
            path = os.path.join(self.guest, *rel.split("/"))
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                return {"req_id": req_id, "error": str(exc)}
            if req.get("mode") == "raw":
                return {
                    "req_id": req_id,
                    "content_b64": base64.b64encode(data).decode("ascii"),
                    "size": len(data),
                }
            return {"req_id": req_id, "content": data.decode("utf-8", "replace")}
        return {"req_id": req_id, "error": f"fake vsock: unknown op {op}"}


@contextmanager
def _sync_context(tmp_path):
    """Fake `_VM` in the registry + patched VsockClient (KVM-free sync).

    Yields `state` = {"guest_root": str, "flaws": {}} for test control.
    """
    guest_root = tmp_path / f"guest-{TASK_ID}"
    guest_root.mkdir(parents=True)
    host_root = tmp_path / f"host-{TASK_ID}"
    host_root.mkdir(parents=True)
    state = {
        "guest_root": str(guest_root),
        "host_root": str(host_root),
        "flaws": {},
    }
    vm = FakeVM(host_root)
    import app.sandbox.guest_agent as _gmod

    orig_client = _gmod.VsockClient
    with _fc._REG_LOCK:
        _fc._REG[TASK_ID] = vm
    _gmod.VsockClient = lambda *a, **k: FakeVsockClient(state)
    try:
        yield state
    finally:
        _gmod.VsockClient = orig_client
        with _fc._REG_LOCK:
            _fc._REG.pop(TASK_ID, None)


def _exec_in_guest(state, workspace):
    """Mirror the host workspace regular files into the fake guest tree.

    `.git` never crosses by definition (the guest tree IS the streamed
    worktree); `__pycache__` likewise.
    """
    import shutil

    g = state["guest_root"]
    for base, dirs, files in os.walk(str(workspace)):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        rel = os.path.relpath(base, str(workspace))
        target_base = os.path.join(g, rel) if rel != "." else g
        os.makedirs(target_base, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(base, name), os.path.join(target_base, name))


def _seed_host_ws(state):
    import shutil
    from pathlib import Path

    ws_path = Path(state["host_root"])
    ws = ws_path
    _git(ws, "init")
    _git(ws, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(ws, "config", "user.email", "t@t.t")
    _git(ws, "config", "user.name", "t")
    _write(str(ws_path / "README.md"), "# repo\n")
    _write(str(ws_path / ".github" / "workflows" / "ci.yml"), _CI_YML_OLD)
    _write(str(ws_path / "run.sh"), "#!/bin/sh\necho ok\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "init")
    _exec_in_guest(state, ws)
    return ws_path


def test_hidden_dir_edit_lands_in_host_git_state(tmp_path):
    """RC acceptance: the ci.yml edit made in the guest appears in the host
    workspace's git status AND changed-files list (#70/#71 failure)."""
    from app.github import publisher as _pub

    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        _write(
            os.path.join(state["guest_root"], ".github", "workflows", "ci.yml"),
            _CI_YML_FIXED,
        )
        out = _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert out["ok"] is True
        assert out["files"] == 1  # only the edited file transferred
        assert out["changed"] == 1 and out["added"] == 0
        assert out["truncated"] is False
        host = (ws / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert host == _CI_YML_FIXED
        status = _git(ws, "status", "--porcelain=v1", "-uall").stdout
        assert ".github/workflows/ci.yml" in status
        assert ".github/workflows/ci.yml" in _pub.changed_files(str(ws))


def test_sync_surfaces_errors_with_path_and_stage(tmp_path):
    """A single-file guest failure fails the WHOLE sync (never skipped)."""
    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        _write(
            os.path.join(state["guest_root"], ".github", "workflows", "ci.yml"),
            _CI_YML_FIXED,
        )
        state["flaws"]["read_error"] = ".github/workflows/ci.yml"
        with pytest.raises(_fc._host.SandboxBlockedError) as ei:
            _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert "read_guest" in str(ei.value)
        assert "ci.yml" in str(ei.value)
        # a failed sync persists NO state snapshot (deletion reconciliation
        # can only run against fully-verified state)
        assert not os.path.exists(_manifest.sync_state_path(str(ws), TASK_ID))


def test_sync_digest_mismatch_fails_closed(tmp_path):
    """Manifest/content disagreement (tamper or race) refuses the write."""
    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        _write(
            os.path.join(state["guest_root"], ".github", "workflows", "ci.yml"),
            _CI_YML_FIXED,
        )
        state["flaws"]["digest_mismatch"] = ".github/workflows/ci.yml"
        with pytest.raises(_fc._host.SandboxBlockedError) as ei:
            _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert "digest mismatch" in str(ei.value)
        host = (ws / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert host == _CI_YML_OLD  # nothing was written on a failed sync


def test_sync_unknown_manifest_op_demands_rootfs_rebuild(tmp_path):
    """Legacy rootfs (no manifest op) is a hard operator error, not a
    silent fallback onto the old best-effort loop."""
    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        state["flaws"]["unknown_op"] = True
        with pytest.raises(_fc._host.SandboxBlockedError) as ei:
            _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert "Rebuild the guest image" in str(ei.value)
        assert "rootfs" in str(ei.value)


def test_mode_preserved_and_unchanged_files_untouched(tmp_path):
    """Digest-identical files are not rewritten (mtime evidence), and an
    existing file's mode survives a content change (`git add Permission
    denied` was caused by the old writer's 0600 temp files)."""
    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        readme_before = os.stat(str(ws / "README.md"))
        run_sh_before_mode = (
            stat.S_IMODE(os.stat(str(ws / "run.sh")).st_mode)
            if os.name == "posix"
            else None
        )
        out = _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert out["ok"] is True
        assert out["files"] == 0  # identical tree: NOTHING transferred
        readme_after = os.stat(str(ws / "README.md"))
        assert readme_before.st_mtime_ns == readme_after.st_mtime_ns
        if os.name != "posix":
            return  # chmod semantics: POSIX-only assertions below
        # new guest file receives the configured safe default mode
        from app.config import settings as _settings

        _write(
            os.path.join(state["guest_root"], ".github", "extra.yml"),
            "on: schedule\n",
        )
        out2 = _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert out2["added"] == 1
        got = stat.S_IMODE(os.stat(str(ws / ".github" / "extra.yml")).st_mode)
        default = stat.S_IMODE(int(_settings.WORKSPACE_FILE_MODE))
        assert got == default
        # change run.sh content in the guest; the host file keeps 0755
        os.chmod(str(ws / "run.sh"), 0o755)
        with open(os.path.join(state["guest_root"], "run.sh"), "a") as fh:
            fh.write("echo more\n")
        out3 = _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert out3["changed"] == 1
        assert stat.S_IMODE(os.stat(str(ws / "run.sh")).st_mode) == 0o755
        assert run_sh_before_mode is not None  # initial mode was preserved


def test_add_change_delete_round_trip_including_hidden_dirs(tmp_path):
    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        g = state["guest_root"]
        # Round 1: add two new files (one inside a hidden dir).
        _write(os.path.join(g, ".fixhub", "marker.txt"), "m\n")
        _write(os.path.join(g, "api.tmp"), "tmp\n")
        r1 = _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert r1["ok"] is True
        assert r1["added"] == 2
        assert (ws / ".fixhub" / "marker.txt").read_text() == "m\n"
        assert (ws / "api.tmp").exists()
        # Round 2: delete one, edit the hidden-dir file, mutate ci.yml.
        os.unlink(os.path.join(g, "api.tmp"))
        _write(os.path.join(g, ".fixhub", "marker.txt"), "m2\n")
        _write(os.path.join(g, ".github", "workflows", "ci.yml"), _CI_YML_FIXED)
        r2 = _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert r2["ok"] is True
        assert r2["deleted"] == 1
        assert r2["changed"] == 2
        assert not (ws / "api.tmp").exists()
        assert (ws / ".fixhub" / "marker.txt").read_text() == "m2\n"
        # verified snapshot holds exactly the reconciled set
        snap = _manifest.load_sync_state(_manifest.sync_state_path(str(ws), TASK_ID))
        assert set(snap) == {
            ".github/workflows/ci.yml",
            ".fixhub/marker.txt",
            "README.md",
            "run.sh",
        }


def test_truncated_manifest_fails_closed_and_never_deletes(tmp_path):
    """An incomplete listing cannot drive reconciliation: explicit failure,
    no writes, no deletions."""
    from app.config import settings as _settings

    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        _write(os.path.join(state["guest_root"], "extra.txt"), "e\n")
        out = _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert out["ok"] is True
        orig = _settings.FC_SYNC_MANIFEST_MAX_FILES
        _settings.FC_SYNC_MANIFEST_MAX_FILES = 1  # forces guest truncation
        try:
            with pytest.raises(_fc._host.SandboxBlockedError) as ei:
                _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
            assert "truncated" in str(ei.value)
        finally:
            _settings.FC_SYNC_MANIFEST_MAX_FILES = orig
        # deletions did NOT run (state preserved; README still on host)
        assert (ws / "README.md").exists()


def _seed_bare_remote(tmp_path, name):
    """Local bare remote + one seed commit on main (no GitHub involved)."""
    bare = tmp_path / name
    _git(tmp_path, "init", "--bare", str(bare))
    seed = tmp_path / f"seed-{name}"
    _git(tmp_path, "clone", str(bare), str(seed))
    _git(seed, "config", "user.email", "t@t.t")
    _git(seed, "config", "user.name", "t")
    (seed / "a.py").write_text("x = 1\n")
    os.makedirs(str(seed / ".github" / "workflows"))
    with open(os.path.join(str(seed), ".github", "workflows", "ci.yml"),
              "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_CI_YML_OLD)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", "-u", "origin", "HEAD:main")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare, _git(bare, "rev-parse", "main").stdout.strip()


def test_hard_sync_failure_blocks_publish_and_records_failed(
    db, tmp_path, monkeypatch
):
    """Transport-level sync failure => task FAILED with a pointed error;
    publish never touches the bare remote."""
    from app.db.models import Repository, Task
    from app.agent import loop as _loop_mod
    from app.agent.loop import LoopResult
    from app.repo import workspace as _ws
    from app.sandbox.sandbox import SandboxBlockedError
    from app.tasks import service as _svc
    import app.sandbox.backend as _sbmod

    bare, base_sha = _seed_bare_remote(tmp_path, "blocked.git")
    db.add(Repository(github_full_name="acme/fp", installation_id=""))
    db.commit()
    repo = db.query(Repository).filter_by(github_full_name="acme/fp").first()
    task = Task(
        repository="acme/fp", repository_id=repo.id, trigger_type="issue",
        status="QUEUED",
    )
    db.add(task)
    db.commit()

    def _fake_run_agent(**k):
        path = _ws.workspace_path(task.id)
        with open(os.path.join(path, "a.py"), "a", encoding="utf-8") as fh:
            fh.write("# agent edit\n")
        return LoopResult(
            True, "edited a.py", 2, 1,
            [{"tool": "write_file", "args": {"path": "a.py"}, "ok": True, "step": 1}],
        )

    monkeypatch.setattr(_loop_mod, "run_agent", _fake_run_agent)
    monkeypatch.setattr(_sbmod, "active_backend_name", lambda: "firecracker")
    monkeypatch.setattr(_fc, "provision", lambda *a, **k: None)  # no KVM here
    monkeypatch.setattr(
        _fc, "sync_guest_to_host",
        lambda **k: (_ for _ in ()).throw(
            SandboxBlockedError("vsock transport died")
        ),
    )
    out = _svc.run_task_inline(task.id, source=str(bare), worker_id="t")
    assert out["status"] == "FAILED", out
    assert "result sync failed" in str(out["error"])
    # publish never started: bare remote main unchanged
    assert _git(bare, "rev-parse", "main").stdout.strip() == base_sha


def test_declared_edit_missing_preserves_workspace_needs_review(
    db, tmp_path, monkeypatch
):
    """The #70/#71 gate: a sync that CLAIMS success but dropped a declared
    edit is caught BEFORE staging -> NEEDS_REVIEW with a preserved workspace."""
    from app.db.models import Repository, Task
    from app.agent import loop as _loop_mod
    from app.agent.loop import LoopResult
    from app.repo import workspace as _ws
    from app.tasks import service as _svc
    import app.github.publisher as _pubmod
    import app.sandbox.backend as _sbmod
    import app.sandbox.firecracker as _fcmod

    bare, base_sha = _seed_bare_remote(tmp_path, "dropped.git")
    db.add(Repository(github_full_name="acme/drop", installation_id=""))
    db.commit()
    repo = db.query(Repository).filter_by(github_full_name="acme/drop").first()
    task = Task(
        repository="acme/drop", repository_id=repo.id, trigger_type="issue",
        status="QUEUED",
    )
    db.add(task)
    db.commit()

    def _fake_run_agent(**k):
        # The workspace sees only the agent's OTHER edit; the declared
        # ci.yml edit "happened" in the VM but will NOT reach the host
        # (silent-drop simulation: the patched sync below keeps claiming ok).
        path = _ws.workspace_path(task.id)
        with open(os.path.join(path, "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("agent notes\n")
        return LoopResult(
            True, "fixed ci and took notes", 3, 2,
            [
                {
                    "tool": "edit_file",
                    "args": {"path": ".github/workflows/ci.yml"},
                    "ok": True,
                    "step": 1,
                }
            ],
        )

    def _legacy_drop_sync(**k):
        return {
            "ok": True, "files": 0, "bytes": 0, "added": 0,
            "changed": 0, "deleted": 0, "truncated": False,
        }

    publish_calls = []

    monkeypatch.setattr(_loop_mod, "run_agent", _fake_run_agent)
    monkeypatch.setattr(_sbmod, "active_backend_name", lambda: "firecracker")
    monkeypatch.setattr(_fcmod, "provision", lambda *a, **k: None)  # no KVM here
    monkeypatch.setattr(_fcmod, "sync_guest_to_host", _legacy_drop_sync)
    monkeypatch.setattr(
        _pubmod, "publish",
        lambda **k: publish_calls.append(k) or {},
    )

    out = _svc.run_task_inline(task.id, source=str(bare), worker_id="t")
    assert out["status"] == "NEEDS_REVIEW", out
    missing = out.get("missing_files") or []
    assert ".github/workflows/ci.yml" in missing
    db.refresh(task)
    row = db.query(Task).filter_by(id=task.id).first()
    assert row.status == "NEEDS_REVIEW"
    assert "workspace_sync_incomplete" in (row.error or "")
    # publish never ran; remote untouched
    assert publish_calls == []
    assert _git(bare, "rev-parse", "main").stdout.strip() == base_sha
    # the workspace is PRESERVED for review (never wiped on this path)
    ws = _ws.workspace_path(task.id)
    assert os.path.isdir(ws)
    assert os.path.isfile(os.path.join(ws, "notes.md"))


def test_publish_pushes_full_fix_to_bare_remote(db, tmp_path, monkeypatch):
    """End-to-end staging path: guest ci.yml fix -> manifest sync -> real
    preflight + git add/commit/push into a local bare remote. This pins the
    whole chain the incident broke WITHOUT GitHub or a VM."""
    from app.github import publisher as _pub
    from app.github import client as _gh_client

    with _sync_context(tmp_path) as state:
        bare, base = _seed_bare_remote(tmp_path, "pushed.git")
        # clone the bare remote into the (empty) fake host workspace so the
        # publish flow has a real origin to push to
        ws = state["host_root"]
        _git(tmp_path, "clone", str(bare), str(ws))
        _git(ws, "config", "user.email", "t@t.t")
        _git(ws, "config", "user.name", "t")
        # the guest edits the workflow file inside a hidden dir
        _write(
            os.path.join(state["guest_root"], ".github", "workflows", "ci.yml"),
            _CI_YML_FIXED,
        )
        out = _fc.sync_guest_to_host(task_id=TASK_ID, workspace=str(ws))
        assert out["ok"] is True and out["changed"] == 1

        monkeypatch.setattr(_gh_client, "list_open_pulls", lambda **k: [])
        monkeypatch.setattr(
            _gh_client,
            "create_pull_request",
            lambda **k: {"number": 42, "url": "https://example.invalid/pr/42"},
        )
        result = _pub.publish(
            path=str(ws),
            full_name="acme/pushed",
            base="main",
            title="fix: ci workflow",
            body="",
            token="x-fake",
        )
        assert result["pr_number"] == 42
        assert result["commit_sha"]
        # the pushed commit on the bare remote contains the workflow fix
        shown = _git(
            bare, "show", f"{_pub.FIX_BRANCH}:.github/workflows/ci.yml"
        ).stdout
        assert shown == _CI_YML_FIXED
        # and the pre-existing file was not damaged (byte-identical intact)
        shown_a = _git(bare, "show", f"{_pub.FIX_BRANCH}:a.py").stdout
        assert shown_a == "x = 1\n"


# --------------------------------------------------------------------------
# Publisher preflight diagnostics
# --------------------------------------------------------------------------


def test_preflight_staging_happy_and_tolerant_of_missing(tmp_path):
    from app.repo import workspace_preflight as _pf

    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        assert _pf.preflight_staging(str(ws), ["README.md", ".github/workflows/ci.yml"]) == []
        # missing files are NOT preflight failures (the declared-edit
        # verifier owns the missing case; this gate proves readability)
        assert _pf.preflight_staging(str(ws), ["ghost.txt"]) == []


def test_preflight_staging_reports_unreadable_file_with_owner(tmp_path, monkeypatch):
    from app.repo import workspace_preflight as _pf

    with _sync_context(tmp_path) as state:
        ws = _seed_host_ws(state)
        if os.name == "posix":
            os.chmod(str(ws / "README.md"), 0o000)
            try:
                failures = _pf.preflight_staging(str(ws), ["README.md"])
            finally:
                os.chmod(str(ws / "README.md"), 0o644)
        else:
            # Windows cannot cheaply simulate unreadable files via ACLs in
            # a portable way; inject the failure at the open() boundary —
            # the preflight contract under test ("name, not skip") is
            # identical in both cases.
            real_open = open

            def _deny(file, *a, **k):
                if os.path.normpath(str(file)) == os.path.normpath(
                    str(ws / "README.md")
                ):
                    raise PermissionError(13, "Permission denied")
                return real_open(file, *a, **k)

            monkeypatch.setattr("builtins.open", _deny)
            failures = _pf.preflight_staging(str(ws), ["README.md"])
        assert failures, "unreadable staging target must be reported"
        assert any(
            "README.md" in f and "not openable" in f for f in failures
        ), failures


def test_preflight_workspace_root_checks(tmp_path):
    from app.repo import workspace_preflight as _pf

    # missing root is fine (bootstrap creates it)
    assert _pf.preflight_workspace_root(str(tmp_path / "does-not-exist")) == []
    # healthy root
    assert _pf.preflight_workspace_root(str(tmp_path)) == []
    # a FILE where the root should be fails with a pointed message
    fake = tmp_path / "root-file"
    fake.write_text("x")
    failures = _pf.preflight_workspace_root(str(fake))
    assert any("not a directory" in f for f in failures)
