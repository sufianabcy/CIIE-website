from django.core.management import call_command

from people.models import Person


def test_first_admin_is_created_once(db, monkeypatch):
    monkeypatch.setenv("FIRST_ADMIN_EMAIL", "Boss@Example.com")
    call_command("ensure_admin")
    boss = Person.objects.get(email="boss@example.com")
    assert boss.is_superuser and boss.groups.filter(name="Admin").exists()
    monkeypatch.setenv("FIRST_ADMIN_EMAIL", "someone.else@example.com")
    call_command("ensure_admin")
    assert not Person.objects.filter(email="someone.else@example.com").exists()


def test_health_check_skips_host_checks(client, settings):
    settings.ALLOWED_HOSTS = ["example.org"]
    response = client.get("/ops/healthz/", HTTP_HOST="10.0.0.5")
    assert response.status_code == 200 and response.content == b"ok"
