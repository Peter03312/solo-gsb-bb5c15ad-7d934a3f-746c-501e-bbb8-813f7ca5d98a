"""End-to-end tests for two-revision comparison snapshots."""
from factories import make_payload


def submit(client, low, high, name="Event"):
    r = client.post(
        "/api/applications", json=make_payload(freq=(low, high), name=name)
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def approve(client, application_id, expected_revision):
    r = client.post(
        f"/api/applications/{application_id}/approve",
        json={"expected_revision": expected_revision},
    )
    assert r.status_code == 200, r.text
    return r.json()


def suspend(client, application_id, expected_revision):
    r = client.post(
        f"/api/applications/{application_id}/suspend",
        json={"expected_revision": expected_revision, "reason": "outage"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def resume(client, application_id, expected_revision):
    r = client.post(
        f"/api/applications/{application_id}/resume",
        json={"expected_revision": expected_revision, "reason": "fixed"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def revoke(client, application_id):
    r = client.post(f"/api/applications/{application_id}/revoke", json={})
    assert r.status_code == 200, r.text
    return r.json()


def emergency_confirm(client, application_id, expected_revision):
    r = client.post(
        "/api/emergency-occupations",
        json={
            "application_id": application_id,
            "expected_revision": expected_revision,
            "reason": "burst",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def emergency_release(client, application_id, expected_revision):
    r = client.post(
        f"/api/emergency-occupations/{application_id}/release",
        json={"expected_revision": expected_revision},
    )
    assert r.status_code == 200, r.text
    return r.json()


def snapshot(client, revision):
    r = client.get(f"/api/revision/{revision}")
    assert r.status_code == 200, r.text
    return r.json()


def approval_record(body, application_id):
    records = [a for a in body["approvals"] if a["application_id"] == application_id]
    assert len(records) == 1
    return records[0]


def waitlist(client, application_id):
    r = client.post(
        f"/api/applications/{application_id}/waitlist",
        json={"reason": "standby"},
    )
    assert r.status_code == 200, r.text


def compare(client, base_revision, target_revision, application_id=None):
    params = {
        "base_revision": base_revision,
        "target_revision": target_revision,
    }
    if application_id is not None:
        params["application_id"] = application_id
    r = client.get("/api/revision/compare", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def event_refs(body):
    return [(event["revision"], event["type"], event.get("application_id"))
            for event in body["differences"]]


class TestRevisionComparison:
    def test_compares_band_sets_status_changes_and_auto_approval(self, client):
        a = submit(client, 100, 110, "A")
        standby = submit(client, 105, 108, "standby")
        approve(client, a, 0)  # revision 1
        waitlist(client, standby)

        suspend(client, a, 1)  # revision 2, standby auto-approved revision 3

        config = client.post(
            "/api/config/guard-band",
            json={"guard_band": 1.0, "expected_revision": 3},
        )
        assert config.status_code == 200, config.text  # revision 4

        before = client.get("/api/revision/current").json()
        body = compare(client, 1, 4)
        after = client.get("/api/revision/current").json()
        assert before == after

        assert body["direction"] == "forward"
        assert body["guard_band"] == {"base": 2.0, "target": 1.0, "changed": True}
        assert {
            k: body["effective_application_ids"][k]
            for k in ("base", "target", "added", "removed")
        } == {
            "base": [a],
            "target": [standby],
            "added": [standby],
            "removed": [a],
        }
        assert {
            k: body["suspended_application_ids"][k]
            for k in ("base", "target", "added", "removed")
        } == {
            "base": [],
            "target": [a],
            "added": [a],
            "removed": [],
        }
        assert body["status_changes"] == [
            {
                "revision": 2,
                "application_id": a,
                "from_status": "approved",
                "to_status": "suspended",
            },
            {
                "revision": 3,
                "application_id": standby,
                "from_status": "pending",
                "to_status": "approved",
            },
        ]
        assert event_refs(body) == [
            (2, "suspension", a),
            (3, "approval", standby),
            (4, "guard_band_change", None),
        ]

        auto_approval = body["approvals"][0]
        assert auto_approval["revision"] == 3
        assert auto_approval["application_id"] == standby
        assert auto_approval["auto_approved"] is True
        assert auto_approval["release_revision"] == 2
        assert auto_approval["guard_band"] == 2.0

        guard_change = body["guard_band_changes"][0]
        assert guard_change["revision"] == 4
        assert guard_change["previous_guard_band"] == 2.0
        assert guard_change["guard_band"] == 1.0

    def test_resumption_and_other_approval_are_replayed_in_order(self, client):
        a = submit(client, 100, 110, "A")
        distant = submit(client, 130, 140, "distant")
        approve(client, a, 0)  # 1
        suspend(client, a, 1)  # 2
        approve(client, distant, 2)  # 3
        resume(client, a, 3)  # 4

        body = compare(client, 2, 4)
        assert event_refs(body) == [
            (3, "approval", distant),
            (4, "resumption", a),
        ]
        assert body["status_changes"] == [
            {
                "revision": 3,
                "application_id": distant,
                "from_status": "pending",
                "to_status": "approved",
            },
            {
                "revision": 4,
                "application_id": a,
                "from_status": "suspended",
                "to_status": "approved",
            },
        ]
        assert body["effective_application_ids"]["target"] == [a, distant]
        assert body["suspended_application_ids"]["target"] == []

    def test_application_filter_scopes_license_events_but_keeps_band_events(
        self, client
    ):
        a = submit(client, 100, 110, "A")
        other = submit(client, 130, 140, "other")
        approve(client, a, 0)  # 1
        approve(client, other, 1)  # 2
        suspend(client, a, 2)  # 3

        body = compare(client, 0, 3, application_id=a)
        assert body["application_id"] == a
        assert body["base"]["application_status"] == "pending"
        assert body["target"]["application_status"] == "suspended"
        assert body["target"]["is_effective"] is False
        assert body["target"]["is_suspended"] is True
        assert event_refs(body) == [
            (1, "approval", a),
            (3, "suspension", a),
        ]
        assert body["status_changes"] == [
            {
                "revision": 1,
                "application_id": a,
                "from_status": "pending",
                "to_status": "approved",
            },
            {
                "revision": 3,
                "application_id": a,
                "from_status": "approved",
                "to_status": "suspended",
            },
        ]

    def test_equal_revisions_return_no_differences(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)

        body = compare(client, 1, 1)
        assert body["differences"] == []
        assert body["status_changes"] == []
        assert {
            k: body["effective_application_ids"][k]
            for k in ("base", "target", "added", "removed")
        } == {
            "base": [a],
            "target": [a],
            "added": [],
            "removed": [],
        }

    def test_backward_comparison_keeps_named_base_and_target_views(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)

        body = compare(client, 1, 0)
        assert body["direction"] == "backward"
        assert body["base_revision"] == 1
        assert body["target_revision"] == 0
        assert event_refs(body) == [
            (1, "approval", a),
        ]
        assert body["base"]["effective_application_ids"] == [a]
        assert body["target"]["effective_application_ids"] == []

    def test_invalid_revisions_and_unknown_application_are_rejected(self, client):
        r = client.get(
            "/api/revision/compare",
            params={"base_revision": 0, "target_revision": 1},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "revision_out_of_range"
        assert r.json()["error"]["details"] == {
            "current_revision": 0,
            "out_of_range_revisions": [1],
        }

        r = client.get(
            "/api/revision/compare",
            params={"base_revision": -1, "target_revision": 0},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "revision_out_of_range"

        r = client.get(
            "/api/revision/compare",
            params={
                "base_revision": 0,
                "target_revision": 0,
                "application_id": 999999,
            },
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


class TestEmbeddedApplicationAsOfReplay:
    """The application embedded in an approval record must describe the grant
    *as of the queried revision*: later decisions mutate the live application
    row but must never reach back into an older revision view."""

    def test_later_suspension_does_not_rewrite_approved_revision_view(
        self, client
    ):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        suspend(client, a, 1)  # revision 2

        # The audited bug: revision 1's approval is still approved at
        # revision 1, even though the live row (and revision 2) is suspended.
        emb1 = approval_record(snapshot(client, 1), a)["application"]
        assert emb1["status"] == "approved"
        assert emb1["approval_revision"] == 1
        assert emb1["suspended_revision"] is None
        assert emb1["force_suspended_by"] is None

        emb2 = approval_record(snapshot(client, 2), a)["application"]
        assert emb2["status"] == "suspended"
        assert emb2["approval_revision"] == 1
        assert emb2["suspended_revision"] == 2
        assert emb2["force_suspended_by"] is None

        # And the live row keeps reflecting real-time state.
        live = client.get(f"/api/applications/{a}").json()
        assert live["status"] == "suspended"
        assert live["suspended_revision"] == 2

    def test_resume_and_revoke_never_rewrite_older_views(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # 1
        suspend(client, a, 1)  # 2
        resume(client, a, 2)  # 3
        revoke(client, a)  # 4

        emb1 = approval_record(snapshot(client, 1), a)["application"]
        emb2 = approval_record(snapshot(client, 2), a)["application"]
        emb3 = approval_record(snapshot(client, 3), a)["application"]
        emb4 = approval_record(snapshot(client, 4), a)["application"]

        assert (emb1["status"], emb1["suspended_revision"]) == (
            "approved",
            None,
        )
        assert (emb2["status"], emb2["suspended_revision"]) == (
            "suspended",
            2,
        )
        assert (emb3["status"], emb3["suspended_revision"]) == (
            "approved",
            None,
        )
        assert (emb4["status"], emb4["suspended_revision"]) == (
            "revoked",
            None,
        )
        assert emb4["approval_revision"] == 1
        assert emb4["force_suspended_by"] is None

        # current snapshot follows the live row
        cur = client.get("/api/revision/current").json()
        assert cur["current_revision"] == 4
        cur_emb = approval_record(cur, a)["application"]
        assert cur_emb["status"] == "revoked"

    def test_revoke_while_suspended_keeps_suspension_marker_as_of(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # 1
        suspend(client, a, 1)  # 2
        revoke(client, a)  # 3

        # The live row keeps suspended_revision as a historical marker; the
        # as-of embedded application must do the same.
        live = client.get(f"/api/applications/{a}").json()
        assert live["status"] == "revoked"
        assert live["suspended_revision"] == 2

        emb = approval_record(snapshot(client, 3), a)["application"]
        assert emb["status"] == "revoked"
        assert emb["suspended_revision"] == 2
        assert emb["force_suspended_by"] is None

    def test_forced_suspension_source_is_replayed_and_release_is_local(
        self, client
    ):
        b = submit(client, 300, 310, "B")
        approve(client, b, 0)  # 1
        em = submit(client, 304, 309, "EM")
        emergency_confirm(client, em, 1)  # B forced at 2, em approved at 3

        emb_forced = approval_record(snapshot(client, 2), b)["application"]
        assert emb_forced["status"] == "suspended"
        assert emb_forced["suspended_revision"] == 2
        assert emb_forced["force_suspended_by"] == em

        emergency_release(client, em, 3)  # em revoked 4, B restored 5
        # The old view keeps the emergency force source ...
        emb_old = approval_record(snapshot(client, 2), b)["application"]
        assert emb_old["force_suspended_by"] == em
        # ... while the restored view is an effective license again.
        emb_now = approval_record(snapshot(client, 5), b)["application"]
        assert emb_now["status"] == "approved"
        assert emb_now["suspended_revision"] is None
        assert emb_now["force_suspended_by"] is None

        em_now = approval_record(snapshot(client, 5), em)["application"]
        assert em_now["status"] == "revoked"
        assert em_now["force_suspended_by"] is None

    def test_blocked_forced_recovery_leaves_ordinary_suspension_view(
        self, client
    ):
        b = submit(client, 300, 310, "B")
        approve(client, b, 0)  # 1
        em = submit(client, 304, 309, "EM")
        emergency_confirm(client, em, 1)  # B forced 2, em approved 3
        # A license inside B's band, approved while B is forced out.
        d = submit(client, 300.5, 301.5, "D")
        approve(client, d, 3)  # 4
        release = emergency_release(client, em, 4)  # em revoked 5
        assert release["restored_application_ids"] == []
        assert release["blocked_application_ids"] == [b]

        emb = approval_record(snapshot(client, 5), b)["application"]
        assert emb["status"] == "suspended"
        assert emb["suspended_revision"] == 2
        # The emergency is over: the remaining suspension is ordinary.
        assert emb["force_suspended_by"] is None

    def test_compare_embeds_as_of_applications_for_each_side(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # 1
        suspend(client, a, 1)  # 2

        # Interval approval records come from the newer side's snapshot, so
        # the embedded application is projected to the newer revision: at
        # revision 2 the revision-1 grant is suspended with marker 2.
        body = compare(client, 0, 2)
        approval = [
            e for e in body["approvals"] if e["application_id"] == a
        ][0]
        assert approval["revision"] == 1
        assert approval["status_as_of"] == "suspended"
        assert approval["suspended_at_revision"] == 2
        assert approval["application"]["status"] == "suspended"
        assert approval["application"]["approval_revision"] == 1
        assert approval["application"]["suspended_revision"] == 2

        # The same comparison read does not mutate anything: before/after the
        # compare, revision 1's own snapshot still reads approved, and the
        # live application stays whatever its current row says (suspended).
        emb1 = approval_record(snapshot(client, 1), a)["application"]
        assert emb1["status"] == "approved"
        assert emb1["suspended_revision"] is None
        live = client.get(f"/api/applications/{a}").json()
        assert live["status"] == "suspended"
        assert live["suspended_revision"] == 2

        # A backward comparison keeps the same immutable as-of projection.
        backward = compare(client, 2, 0)
        backward_approval = [
            e for e in backward["approvals"] if e["application_id"] == a
        ][0]
        assert backward_approval["application"]["status"] == "suspended"
        assert (
            backward_approval["application"]["suspended_revision"] == 2
        )
