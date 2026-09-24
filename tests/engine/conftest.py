import os
import time

import pytest


@pytest.fixture(autouse=True)
def utc_process_tz():
    """Run every engine scenario with the process timezone at UTC.

    freezegun's naive `now()` ignores the process timezone but FakeWorld's
    `.astimezone()` does not, so off UTC the scripted local times and the
    +00:00 sun/marker times drift apart (scenarios fail, one never ends). CI and
    the Docker image are UTC already; this makes a native run anywhere match
    them and the golden fixtures' timestamps.
    """
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()
