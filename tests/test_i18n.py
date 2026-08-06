def test_italian_translation_renders(client):
    resp = client.get("/?locale=it")
    assert resp.status_code == 200
    # "Sign in" -> "Accedi" when the Italian catalog is compiled.
    assert "Accedi" in resp.text


def test_english_default(client):
    resp = client.get("/?locale=en")
    assert resp.status_code == 200
    assert "Sign in" in resp.text
