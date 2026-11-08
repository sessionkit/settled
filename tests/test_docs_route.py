"""GET /docs/NAME.md serves one of the kit's installed documents as markdown, from
$SETTLED_DOCS (else ~/.sessionkit/docs); a missing one is a 404 that names the path, so
the screen can say so. Nothing but a .md file at the top of that folder is served."""
import json
import pytest

from settled import web


class Conn:
    def respond(self, code, text):
        return ("respond", code, text)


class Req:
    def __init__(self, path):
        self.path = path
        from websockets.datastructures import Headers
        self.headers = Headers()


def page(path):
    return web.page(Conn(), Req(path))


@pytest.fixture
def docs(tmp_path, monkeypatch):
    (tmp_path / "what-listens.md").write_text("# What listens\n\nThey listen to `<microphone>`.\n")
    (tmp_path / "secret.txt").write_text("no")
    monkeypatch.setenv("SETTLED_DOCS", str(tmp_path))
    return tmp_path


def test_the_document_is_served_as_markdown(docs):
    r = page("/docs/what-listens.md")
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/markdown")
    assert b"`<microphone>`" in r.body and r.headers["Cache-Control"] == "no-store"


def test_a_missing_document_names_the_path_it_would_be_at(docs):
    r = page("/docs/who-hears.md")
    assert r.status_code == 404
    assert json.loads(r.body)["missing"] == str(docs / "who-hears.md")


def test_only_markdown_at_the_top_of_the_folder(docs):
    for p in ("/docs/secret.txt", "/docs/../what-listens.md", "/docs/sub/what-listens.md"):
        assert page(p).status_code == 404, p
