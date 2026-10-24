"""
Serving under /ATIP/ (docs/HOSTED_DEPLOYMENT.md section 4).

Behind a reverse proxy that strips the prefix, FastAPI must be told it
(ATIP_ROOT_PATH or config.json dashboard_root_path), or every generated URL and
the page's own fetch() calls resolve against "/" and 404. The smoke run (TEST_SPEC.md
S5) checks a live server's openapi.json; this pins the normalisation it relies on.
"""

import json


def test_the_prefix_is_normalised_to_one_leading_slash(monkeypatch, tmp_path):
    from dashboard import security
    for raw, want in (("ATIP", "/ATIP"), ("/ATIP/", "/ATIP"), ("//ATIP//", "/ATIP"), (" /ATIP ", "/ATIP"),
                      ("/", ""), ("", "")):
        monkeypatch.setenv("ATIP_ROOT_PATH", raw)
        assert security.dashboard_root_path() == want, raw


def test_config_json_supplies_the_prefix_when_the_environment_does_not(monkeypatch, tmp_path):
    from dashboard import security
    monkeypatch.delenv("ATIP_ROOT_PATH", raising=False)
    assert security.dashboard_root_path() == ""                    # a plain local run is served at /
    security.CONFIG_PATH.write_text(json.dumps({"dashboard_root_path": "ATIP/"}), encoding="utf-8")
    assert security.dashboard_root_path() == "/ATIP"
    monkeypatch.setenv("ATIP_ROOT_PATH", "/other")
    assert security.dashboard_root_path() == "/other", "the environment wins"
