import json

import pytest

from argus_herald.http import HttpError
from argus_herald.pypi import fetch_release


class PyPI:
    """The versioned endpoint answers `versioned`; the project page describes `latest`."""

    def __init__(self, versioned, latest="0.3.0", yanked=True):
        self.versioned, self.latest, self.yanked = versioned, latest, yanked

    def request(self, method, url, body=None, headers=None):
        if url == "https://pypi.org/pypi/argus-atlas/json":
            info = {"name": "argus-atlas", "version": self.latest, "summary": "latest", "yanked": self.yanked}
            return 200, json.dumps({"info": info}).encode()
        return self.versioned, b"{}"


def test_the_latest_release_is_never_passed_off_as_another_version():
    with pytest.raises(HttpError) as caught:
        fetch_release(PyPI(404), "argus-atlas", "0.2.0")
    assert caught.value.status == 404


def test_a_server_error_is_not_answered_from_the_project_page():
    with pytest.raises(HttpError) as caught:
        fetch_release(PyPI(503, latest="0.2.0"), "argus-atlas", "0.2.0")
    assert caught.value.status == 503


def test_the_project_page_stands_in_when_it_is_the_same_version():
    release = fetch_release(PyPI(404, latest="0.2.0", yanked=False), "argus-atlas", "0.2.0")
    assert (release.version, release.summary, release.yanked) == ("0.2.0", "latest", False)
    assert release.url == "https://pypi.org/project/argus-atlas/0.2.0/"
