import json
import os
from pathlib import Path

import pytest
import urllib3
from django.conf import settings
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django_scopes import scope
from jsonschema import validate
from lxml import etree

from pretalx.agenda.tasks import export_schedule_html
from pretalx.common.tasks import regenerate_css
from pretalx.event.models import Event


@pytest.mark.skipif(
    "CI" not in os.environ or not os.environ["CI"],
    reason="No need to bother with this outside of CI.",
)
def test_schedule_xsd_is_up_to_date():
    """If this test fails:

    http -d https://raw.githubusercontent.com/voc/schedule/master/validator/xsd/schedule.xml.xsd >! tests/fixtures/schedule.xsd
    """
    http = urllib3.PoolManager()
    response = http.request(
        "GET",
        "https://raw.githubusercontent.com/voc/schedule/master/validator/xsd/schedule.xml.xsd",
    )
    assert response.status == 200
    path = Path(__file__).parent / "../fixtures/schedule.xsd"
    with open(path) as schema:
        schema_content = schema.read()
    assert response.data.decode() == schema_content


@pytest.mark.skipif(
    "CI" not in os.environ or not os.environ["CI"],
    reason="No need to bother with this outside of CI.",
)
def test_schedule_json_schema_is_up_to_date():
    """If this test fails:

    http -d https://raw.githubusercontent.com/voc/schedule/master/validator/json/schema.json >! tests/fixtures/schedule.json
    """
    http = urllib3.PoolManager()
    response = http.request(
        "GET",
        "https://raw.githubusercontent.com/voc/schedule/master/validator/json/schema.json",
    )
    assert response.status == 200
    path = Path(__file__).parent / "../fixtures/schedule.json"
    with open(path) as schema:
        schema_content = schema.read()
    assert response.data.decode() == schema_content


@pytest.mark.django_db
def test_schedule_frab_xml_export(
    slot,
    client,
    django_assert_max_num_queries,
    schedule_schema_xml,
    break_slot,
):
    with django_assert_max_num_queries(12):
        response = client.get(
            reverse(
                "agenda:export.schedule.xml",
                kwargs={"event": slot.submission.event.slug},
            ),
            follow=True,
        )
    assert response.status_code == 200, str(response.content.decode())
    assert "ETag" in response

    content = response.content.decode()
    assert slot.submission.title in content
    assert slot.submission.urls.public.full() in content

    parser = etree.XMLParser(schema=schedule_schema_xml)
    etree.fromstring(
        response.content, parser
    )  # Will raise if the schedule does not match the schema
    with django_assert_max_num_queries(9):
        response = client.get(
            reverse(
                "agenda:export.schedule.xml",
                kwargs={"event": slot.submission.event.slug},
            ),
            HTTP_IF_NONE_MATCH=response["ETag"],
            follow=True,
        )
    assert response.status_code == 304


@pytest.mark.django_db
def test_schedule_frab_xml_export_control_char(
    slot, client, django_assert_max_num_queries
):
    slot.submission.description = "control char: \a"
    slot.submission.save()

    with django_assert_max_num_queries(11):
        response = client.get(
            reverse(
                "agenda:export.schedule.xml",
                kwargs={"event": slot.submission.event.slug},
            ),
            follow=True,
        )

    parser = etree.XMLParser()
    etree.fromstring(response.content, parser)


@pytest.mark.django_db
def test_schedule_frab_json_export(
    slot,
    break_slot,
    answered_choice_question,
    personal_answer,
    client,
    django_assert_max_num_queries,
    orga_user,
    schedule_schema_json,
):
    with django_assert_max_num_queries(14):
        regular_response = client.get(
            reverse(
                "agenda:export.schedule.json",
                kwargs={"event": slot.submission.event.slug},
            ),
            follow=True,
        )
    client.force_login(orga_user)
    with django_assert_max_num_queries(23):
        orga_response = client.get(
            reverse(
                "agenda:export.schedule.json",
                kwargs={"event": slot.submission.event.slug},
            ),
            follow=True,
        )
    assert regular_response.status_code == 200
    assert orga_response.status_code == 200

    regular_content = regular_response.content.decode()
    orga_content = orga_response.content.decode()

    assert slot.submission.title in regular_content
    assert slot.submission.title in orga_content
    assert personal_answer.answer in orga_content
    assert personal_answer.answer not in regular_content

    regular_content = json.loads(regular_content)
    orga_content = json.loads(orga_content)
    assert regular_content["schedule"]
    assert orga_content["schedule"]

    assert regular_content != orga_content

    validate(instance=regular_content, schema=schedule_schema_json)
    validate(instance=orga_content, schema=schedule_schema_json)


@pytest.mark.django_db
def test_schedule_frab_xcal_export(
    slot, client, django_assert_max_num_queries, break_slot
):
    with django_assert_max_num_queries(11):
        response = client.get(
            reverse(
                "agenda:export.schedule.xcal",
                kwargs={"event": slot.submission.event.slug},
            ),
            follow=True,
        )
    assert response.status_code == 200

    content = response.content.decode()
    assert slot.submission.title in content


@pytest.mark.django_db
def test_schedule_ical_export(slot, client, django_assert_max_num_queries):
    with django_assert_max_num_queries(14):
        response = client.get(
            reverse(
                "agenda:export.schedule.ics",
                kwargs={"event": slot.submission.event.slug},
            ),
            follow=True,
        )
    assert response.status_code == 200

    content = response.content.decode()
    assert slot.submission.title in content


@pytest.mark.django_db
def test_schedule_single_ical_export(slot, client, django_assert_max_num_queries):
    with django_assert_max_num_queries(13):
        response = client.get(slot.submission.urls.ical, follow=True)
    assert response.status_code == 200

    content = response.content.decode()
    assert slot.submission.title in content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "exporter",
    ("schedule.xml", "schedule.json", "schedule.xcal", "schedule.ics", "feed"),
)
def test_schedule_export_nonpublic(
    exporter, slot, client, django_assert_max_num_queries
):
    slot.submission.event.is_public = False
    slot.submission.event.save()
    exporter = "feed" if exporter == "feed" else f"export.{exporter}"

    with django_assert_max_num_queries(5):
        response = client.get(
            reverse(f"agenda:{exporter}", kwargs={"event": slot.submission.event.slug}),
            follow=True,
        )
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    "exporter",
    ("schedule.xml", "schedule.json", "schedule.xcal", "schedule.ics", "feed"),
)
def test_schedule_export_public(exporter, slot, client, django_assert_max_num_queries):
    exporter = "feed" if exporter == "feed" else f"export.{exporter}"

    with django_assert_max_num_queries(13):
        response = client.get(
            reverse(f"agenda:{exporter}", kwargs={"event": slot.submission.event.slug}),
            follow=True,
        )
    assert response.status_code == 200


@pytest.mark.django_db
def test_schedule_speaker_ical_export(
    slot, other_slot, client, django_assert_max_num_queries
):
    with scope(event=slot.submission.event):
        speaker = slot.submission.speakers.all()[0]
        profile = speaker.profiles.get(event=slot.event)
    with django_assert_max_num_queries(16):
        response = client.get(profile.urls.talks_ical, follow=True)
    assert response.status_code == 200

    content = response.content.decode()
    assert slot.submission.title in content
    assert other_slot.submission.title not in content


@pytest.mark.django_db
def test_feed_view(slot, client, django_assert_max_num_queries, schedule):
    with django_assert_max_num_queries(10):
        response = client.get(slot.submission.event.urls.feed)
    assert response.status_code == 200
    assert schedule.version in response.content.decode()


@pytest.mark.django_db
def test_html_export_event_required():
    from django.core.management import (  # Import here to avoid overriding mocks
        call_command,
    )

    with pytest.raises(CommandError) as excinfo:
        call_command("export_schedule_html")

    assert "the following arguments are required: event" in str(excinfo.value)


@pytest.mark.django_db
def test_html_export_event_unknown(event):
    from django.core.management import (  # Import here to avoid overriding mocks
        call_command,
    )

    with pytest.raises(CommandError) as excinfo:
        call_command("export_schedule_html", "foobar222")
    assert 'Could not find event with slug "foobar222"' in str(excinfo.value)
    export_schedule_html(event_id=22222)
    export_schedule_html(event_id=event.pk)


@pytest.mark.django_db
@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "lalala",
        }
    }
)
def test_html_export_release_without_celery(mocker, event):
    with scope(event=event):
        event.cache.delete("rebuild_schedule_export")
        assert not event.cache.get("rebuild_schedule_export")
        event.feature_flags["export_html_on_release"] = True
        event.save()
        event.wip_schedule.freeze(name="ohaio means hello")
        assert event.cache.get("rebuild_schedule_export")


@pytest.mark.django_db
@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "lalala",
        }
    },
    HAS_CELERY=True,
)
def test_html_export_release_with_celery(mocker, event):
    mocker.patch("django.core.management.call_command")

    from django.core.management import (  # Import here to avoid overriding mocks
        call_command,
    )

    with scope(event=event):
        event.cache.delete("rebuild_schedule_export")
        event.feature_flags["export_html_on_release"] = True
        event.save()
        event.wip_schedule.freeze(name="ohaio means hello")
        assert not event.cache.get("rebuild_schedule_export")

    call_command.assert_called_with("export_schedule_html", event.slug, "--zip")


@pytest.mark.django_db
def test_html_export_release_disabled(mocker, event):
    mocker.patch("django.core.management.call_command")

    from django.core.management import (  # Import here to avoid overriding mocks
        call_command,
    )

    with scope(event=event):
        event.feature_flags["export_html_on_release"] = False
        event.save()
        event.wip_schedule.freeze(name="ohaio means hello")

    call_command.assert_not_called()


@pytest.mark.django_db
def test_html_export_language(event, slot):
    from django.core.management import (  # Import here to avoid overriding mocks
        call_command,
    )

    event.locale = "de"
    event.locale_array = "de,en"
    event.save()
    with override_settings(COMPRESS_ENABLED=True, COMPRESS_OFFLINE=True):
        call_command("rebuild")
        call_command("export_schedule_html", event.slug)

    schedule_html = open(
        settings.HTMLEXPORT_ROOT / "test" / "test/schedule/index.html"
    ).read()
    assert "Kontakt" in schedule_html
    assert "locale/set" not in schedule_html  # bug #494


@pytest.mark.django_db
def test_schedule_export_schedule_html_task(mocker, event, slot):
    mocker.patch("django.core.management.call_command")
    from django.core.management import (  # Import here to avoid overriding mocks
        call_command,
    )

    export_schedule_html.apply_async(kwargs={"event_id": event.id})

    call_command.assert_called_with("export_schedule_html", event.slug, "--zip")


@pytest.mark.django_db
def test_schedule_export_schedule_html_task_nozip(mocker, event, slot):
    mocker.patch("django.core.management.call_command")
    from django.core.management import (  # Import here to avoid overriding mocks
        call_command,
    )

    export_schedule_html.apply_async(kwargs={"event_id": event.id, "make_zip": False})
    call_command.assert_called_with("export_schedule_html", event.slug)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "lalala",
        }
    }
)
@pytest.mark.django_db
def test_schedule_orga_trigger_export_without_celery(
    orga_client, django_assert_max_num_queries, event
):
    event.cache.delete("rebuild_schedule_export")
    assert not event.cache.get("rebuild_schedule_export")
    with django_assert_max_num_queries(39):
        response = orga_client.post(
            event.orga_urls.schedule_export_trigger, follow=True
        )
    assert response.status_code == 200
    event.refresh_from_db()
    assert event.cache.get("rebuild_schedule_export")


@pytest.mark.django_db
@override_settings(HAS_CELERY=True)
def test_schedule_orga_trigger_export_with_celery(
    mocker, orga_client, django_assert_max_num_queries, event
):
    mocker.patch("pretalx.agenda.tasks.export_schedule_html.apply_async")
    from pretalx.agenda.tasks import export_schedule_html

    with django_assert_max_num_queries(39):
        response = orga_client.post(
            event.orga_urls.schedule_export_trigger, follow=True
        )
    assert response.status_code == 200
    export_schedule_html.apply_async.assert_called_once_with(
        kwargs={"event_id": event.id}
    )


@pytest.mark.django_db
def test_html_export_full(
    event,
    other_event,
    slot,
    confirmed_resource,
    canceled_talk,
    orga_client,
    django_assert_max_num_queries,
):
    from django.core.management import (  # Import here to avoid overriding mocks
        call_command,
    )

    event.primary_color = "#111111"
    event.is_public = False
    event.save()
    other_event.primary_color = "#222222"
    other_event.save()

    with override_settings(COMPRESS_ENABLED=True, COMPRESS_OFFLINE=True):
        call_command("rebuild")
        regenerate_css(event.pk)
        regenerate_css(other_event.pk)
        event = Event.objects.get(slug=event.slug)
        assert event.settings.agenda_css_file
        call_command("export_schedule_html", event.slug, "--zip")

    paths = [
        "static/common/img/icons/favicon.ico",
        f'media/test/{event.settings.agenda_css_file.split("/")[-1]}',
        "test/schedule/index.html",
        "test/schedule/export/schedule.json",
        "test/schedule/export/schedule.xcal",
        "test/schedule/export/schedule.xml",
        "test/schedule/export/schedule.ics",
        *[
            f"test/speaker/{speaker.code}/index.html"
            for speaker in slot.submission.speakers.all()
        ],
        f"test/talk/{slot.submission.code}/index.html",
        f"test/talk/{slot.submission.code}.ics",
        confirmed_resource.resource.url.lstrip("/"),
    ]

    for path in paths:
        full_path = settings.HTMLEXPORT_ROOT / "test" / path
        assert full_path.exists()

    for path in (settings.HTMLEXPORT_ROOT / "test/media/").glob("*"):
        path = str(path)
        assert event.slug in path
        assert other_event.slug not in path

    full_path = settings.HTMLEXPORT_ROOT / "test.zip"
    assert full_path.exists()

    # views and templates are the same for export and online viewing, so a naive test is enough here
    talk_html = (
        (
            settings.HTMLEXPORT_ROOT
            / "test"
            / f"test/talk/{slot.submission.code}/index.html"
        )
        .open()
        .read()
    )
    assert talk_html.count(slot.submission.title) >= 2

    speaker = slot.submission.speakers.all()[0]
    schedule_html = (
        (settings.HTMLEXPORT_ROOT / "test" / "test/schedule/index.html").open().read()
    )
    assert "Contact us" in schedule_html  # locale
    assert canceled_talk.submission.title not in schedule_html

    schedule_json = json.load(
        (settings.HTMLEXPORT_ROOT / "test/test/schedule/export/schedule.json").open()
    )
    assert schedule_json["schedule"]["conference"]["title"] == event.name

    schedule_ics = (
        (settings.HTMLEXPORT_ROOT / "test/test/schedule/export/schedule.ics")
        .open()
        .read()
    )
    assert slot.submission.code in schedule_ics
    assert canceled_talk.submission.code not in schedule_ics

    schedule_xcal = (
        (settings.HTMLEXPORT_ROOT / "test/test/schedule/export/schedule.xcal")
        .open()
        .read()
    )
    assert event.slug in schedule_xcal
    assert speaker.name in schedule_xcal

    schedule_xml = (
        (settings.HTMLEXPORT_ROOT / "test/test/schedule/export/schedule.xml")
        .open()
        .read()
    )
    with scope(event=slot.submission.event):
        assert slot.submission.title in schedule_xml
        assert canceled_talk.frab_slug not in schedule_xml
        assert str(canceled_talk.uuid) not in schedule_xml

    talk_ics = (
        (settings.HTMLEXPORT_ROOT / f"test/test/talk/{slot.submission.code}.ics")
        .open()
        .read()
    )
    assert slot.submission.title in talk_ics
    assert event.is_public is False

    with django_assert_max_num_queries(10):
        response = orga_client.get(
            event.orga_urls.schedule_export_download, follow=True
        )
    assert response.status_code == 200
    streaming_content = getattr(response, "streaming_content", None)
    if streaming_content:
        assert len(b"".join(response.streaming_content)) > 100_000  # 100 KB


@pytest.mark.django_db
def test_speaker_csv_export(slot, orga_client, django_assert_max_num_queries):
    with django_assert_max_num_queries(18):
        response = orga_client.get(
            reverse(
                "agenda:export",
                kwargs={"event": slot.submission.event.slug, "name": "speakers.csv"},
            ),
            follow=True,
        )
    assert response.status_code == 200, str(response.content.decode())
    assert slot.submission.speakers.first().name in response.content.decode()


@pytest.mark.django_db
def test_empty_speaker_csv_export(orga_client, django_assert_max_num_queries, event):
    with django_assert_max_num_queries(13):
        response = orga_client.get(
            reverse(
                "agenda:export",
                kwargs={"event": event.slug, "name": "speakers.csv"},
            ),
            follow=True,
        )
    assert response.status_code == 200, str(response.content.decode())
    assert len(response.content.decode()) < 100


@pytest.mark.django_db
def test_submission_question_csv_export(
    slot,
    orga_client,
    answer,
    answered_choice_question,
    impersonal_answer,
    personal_answer,
):
    response = orga_client.get(
        reverse(
            "agenda:export",
            kwargs={
                "event": slot.submission.event.slug,
                "name": "submission-questions.csv",
            },
        ),
        follow=True,
    )
    assert response.status_code == 200, str(response.content.decode())
    assert slot.submission.title in response.content.decode()


@pytest.mark.django_db
def test_speaker_question_csv_export(
    slot,
    orga_client,
    answer,
    answered_choice_question,
    impersonal_answer,
    personal_answer,
):
    response = orga_client.get(
        reverse(
            "agenda:export",
            kwargs={
                "event": slot.submission.event.slug,
                "name": "speaker-questions.csv",
            },
        ),
        follow=True,
    )
    assert response.status_code == 200, str(response.content.decode())
    assert slot.submission.speakers.first().name in response.content.decode()


@pytest.mark.django_db
def test_wrong_export(
    slot,
    orga_client,
):
    response = orga_client.get(
        reverse(
            "agenda:export",
            kwargs={
                "event": slot.submission.event.slug,
                "name": "wrong",
            },
        ),
        follow=True,
    )
    assert response.status_code == 404
