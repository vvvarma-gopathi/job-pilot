from jobpilot_discovery.utils import clean_html, fingerprint, is_public_https_url, normalize_url


def test_clean_html_decodes_entities():
    assert clean_html("&lt;p&gt;Python &amp; FastAPI&lt;/p&gt;") == "Python & FastAPI"


def test_normalize_url_removes_tracking():
    assert normalize_url("https://EXAMPLE.com/jobs/1/?utm_source=x#top") == "https://example.com/jobs/1"


def test_fingerprint_is_case_insensitive():
    assert fingerprint("Python Developer", "Acme", "Hyderabad") == fingerprint("python developer", "ACME", "hyderabad")


def test_external_url_guard_rejects_plain_http_and_private_hosts():
    assert is_public_https_url("https://jobs.example.com/role")
    assert not is_public_https_url("http://jobs.example.com/role")
    assert not is_public_https_url("https://127.0.0.1/role")
