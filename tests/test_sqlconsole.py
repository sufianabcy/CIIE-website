import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from events.models import Event
from sqlconsole import engine
from sqlconsole.models import SqlRun

URL = "/admin/sqlconsole/sqlrun/"

# The console needs a real transaction of its own (SET TRANSACTION READ ONLY must come first).
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def superuser(make_person):
    return make_person(full_name="Root", is_staff=True, is_superuser=True)


@pytest.fixture
def portal_admin(make_person):
    person = make_person(full_name="Admin Only", is_staff=True)
    person.groups.add(Group.objects.get_or_create(name="Admin")[0])
    return person


def test_only_admins_reach_the_console_and_it_is_on_the_admin_home(client, make_person, portal_admin):
    coordinator = make_person(is_staff=True)
    coordinator.groups.add(Group.objects.get_or_create(name="Coordinator")[0])
    client.force_login(coordinator)
    assert client.get(URL).status_code == 403
    client.force_login(portal_admin)
    assert client.get(URL).status_code == 200
    assert client.get("/admin/sql/")["Location"] == URL
    assert b"SQL console" in client.get(reverse("admin:index")).content


def test_read_query_runs_logs_and_limits(client, portal_admin, event):
    client.force_login(portal_admin)
    page = client.post(URL, {"sql": "SELECT title FROM events_event", "action": "read"})
    assert event.title.encode() in page.content
    run = SqlRun.objects.get()
    assert (run.user, run.mode, run.rows, run.error) == (portal_admin, SqlRun.Mode.READ, 1, "")
    page = client.post(URL, {"sql": "SELECT generate_series(1, 50)", "action": "read", "limit": "10"})
    assert b"stopped at the limit" in page.content
    csv = client.get(URL, {"download": "csv", "run": run.pk})
    assert event.title in csv.content.decode()


@pytest.mark.parametrize("sql,message", [
    ("DELETE FROM events_event", "one SELECT"),
    ("SELECT 1; SELECT 2", "One statement"),
    ("SELECT password FROM people_person", "sign-in secrets"),
    ("SELECT * FROM django_session", "sign-in secrets"),
])
def test_reads_refuse_writes_multiple_statements_and_secrets(client, portal_admin, sql, message):
    client.force_login(portal_admin)
    page = client.post(URL, {"sql": sql, "action": "read"})
    assert message.encode() in page.content
    assert SqlRun.objects.get().error


def test_read_only_transaction_blocks_sneaky_writes(portal_admin, event):
    with pytest.raises(Exception, match="read-only"):
        engine.run_read("WITH gone AS (DELETE FROM events_question RETURNING 1) SELECT * FROM gone")


def test_statement_timeout(portal_admin):
    with pytest.raises(Exception, match="statement timeout"):
        engine.TIMEOUT = "200ms"
        try:
            engine.run_read("SELECT pg_sleep(2)")
        finally:
            engine.TIMEOUT = "10s"


def test_password_hashes_are_masked(portal_admin):
    columns, rows, _, _ = engine.run_read("SELECT email, 'pbkdf2_sha256$1$abc' AS x FROM people_person")
    assert rows[0][1] == engine.MASK


def test_non_superusers_cannot_write(client, portal_admin, event):
    client.force_login(portal_admin)
    assert client.post(URL, {"sql": f"UPDATE events_event SET title='X' WHERE id={event.pk}", "action": "dry_run"}).status_code == 403
    event.refresh_from_db()
    assert event.title != "X"


def test_dry_run_changes_nothing_then_confirm_commits_with_history(client, superuser, make_event):
    events = [make_event(title=f"E{i}") for i in range(3)]
    sql = "UPDATE events_event SET venue = 'Hall 9' WHERE title IN ('E0', 'E1')"
    client.force_login(superuser)
    page = client.post(URL, {"sql": sql, "action": "dry_run"})
    assert b"This would change 2 rows" in page.content and b"Hall 9" in page.content
    assert not Event.objects.filter(venue="Hall 9").exists()
    wrong = client.post(URL, {"sql": sql, "action": "commit", "confirm": "3"})
    assert b"Type 2" in wrong.content and not Event.objects.filter(venue="Hall 9").exists()
    client.post(URL, {"sql": sql, "action": "commit", "confirm": "2"})
    assert Event.objects.filter(venue="Hall 9").count() == 2
    assert SqlRun.objects.filter(mode=SqlRun.Mode.WRITE, committed=False).get().error  # the wrong count was logged too
    run = SqlRun.objects.filter(mode=SqlRun.Mode.WRITE, committed=True).get()
    assert run.committed and run.rows == 2
    from pghistory.models import Events

    change = Events.objects.filter(pgh_obj_model="events.Event", pgh_obj_id=str(events[0].pk)).order_by("-pgh_created_at").first()
    assert change.pgh_context["user"] == superuser.pk and change.pgh_context["source"] == "sql-console"


def test_commit_needs_a_dry_run_of_the_same_statement(client, superuser, event):
    client.force_login(superuser)
    page = client.post(URL, {"sql": f"DELETE FROM events_question WHERE event_id={event.pk}", "action": "commit", "confirm": "CONFIRM"})
    assert b"dry run for this exact statement" in page.content


@pytest.mark.parametrize("sql,message", [
    ("DROP TABLE events_event", "one INSERT"),
    ("UPDATE events_event SET title='a'; DROP TABLE events_event", "One statement"),
    ("UPDATE auth_group SET name='x'", "protected"),
    ("DELETE FROM django_session", "sign-in secrets"),
    ("UPDATE people_person SET password='x'", "sign-in secrets"),
    ("DELETE FROM events_eventevent", "protected"),
    ("DELETE FROM pghistory_context", "protected"),
])
def test_writes_refuse_ddl_multiple_statements_and_protected_tables(client, superuser, sql, message):
    client.force_login(superuser)
    page = client.post(URL, {"sql": sql, "action": "dry_run"})
    assert message.encode() in page.content, page.content[-3000:]
