#!/usr/bin/env python3
"""Production-container regression gates for real-world EPUB structures."""
from pathlib import Path
import base64
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/app")
from server import PipelineError, epubcheck, inventory, validate, validation_summary

CONTAINER = b'''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def page(body):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fixture</title></head><body>{body}</body></html>'''.encode()


def write_epub(path, pages):
    manifest = []
    spine = []
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/pixel.png", PIXEL)
        manifest.append('<item id="pixel" href="pixel.png" media-type="image/png"/>')
        for index, (name, body) in enumerate(pages):
            ident = f"page-{index}"
            manifest.append(f'<item id="{ident}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="{ident}"/>')
            archive.writestr(f"OEBPS/{name}", page(body))
        opf = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:validator-fixture</dc:identifier>
    <dc:title>Validator fixture</dc:title><dc:language>pt</dc:language>
  </metadata>
  <manifest>{''.join(manifest)}</manifest><spine>{''.join(spine)}</spine>
</package>'''
        archive.writestr("OEBPS/content.opf", opf.encode())


def rewrite_epub(source, target, transform):
    with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(target, "w") as outgoing:
        for info in incoming.infolist():
            data = transform(info.filename, incoming.read(info.filename))
            outgoing.writestr(info, data)


def require_pipeline_error(code, operation):
    try:
        operation()
    except PipelineError as error:
        assert error.code == code, (error.code, str(error))
    else:
        raise AssertionError(f"Expected {code}")


def main():
    with tempfile.TemporaryDirectory(prefix="validator-regressions-") as folder:
        root = Path(folder)

        portuguese = root / "a-rapariga-sem-tempo.epub"
        write_epub(portuguese, [
            ("cover.xhtml", '<svg xmlns="http://www.w3.org/2000/svg"><image href="pixel.png"/></svg>'),
            ("ARaparigaSemTempo_ebook.xhtml", ""),
            ("chapter.xhtml", "<h1>Capítulo um</h1><p>Este é o conteúdo real do livro.</p>"),
        ])
        report = inventory(portuguese)
        assert report["readableSpineItems"] == 2
        assert report["spineVisualItems"] == 1
        assert report["emptyChapters"] == ["OEBPS/ARaparigaSemTempo_ebook.xhtml"]

        # Manifest hrefs are IRIs. A percent-escaped path, fragment, and a
        # canonically equivalent decomposed Unicode ZIP name must all resolve
        # to the same case-sensitive archive member.
        iri_source = root / "manifest-iri.epub"
        decomposed = unicodedata.normalize("NFD", "capítulo.xhtml")
        write_epub(iri_source, [(decomposed, "<p>Conteúdo real.</p>")])
        iri_rewritten = root / "manifest-iri-rewritten.epub"
        def encode_manifest_iri(name, data):
            if name.lower().endswith(".opf"):
                text = data.decode("utf-8")
                text = text.replace(decomposed, "cap%C3%ADtulo.xhtml#inicio")
                return text.encode("utf-8")
            return data
        rewrite_epub(iri_source, iri_rewritten, encode_manifest_iri)
        assert inventory(iri_rewritten)["readableSpineItems"] == 1

        # Reproduce the production failure: a real-sized inventory exceeds the
        # old 7,000-character header, while the new summary remains valid JSON.
        large_inventory = dict(report)
        large_inventory["chapters"] = [f"OEBPS/Section{index:04}.xhtml" for index in range(200)]
        large_inventory["resources"] = [[name, "application/xhtml+xml"] for name in large_inventory["chapters"]]
        full_report = {"source": large_inventory, "output": large_inventory, "epubcheck": {"version": "5.3.0", "messages": 0}}
        assert len(str(full_report)) > 7000
        summary, encoded = validation_summary(full_report)
        assert len(encoded.encode("ascii")) < 4096
        assert summary["output"]["spineCount"] == report["spineCount"]
        assert summary["output"]["blankSpineItems"] == 1

        image_only = root / "image-only.epub"
        write_epub(image_only, [
            ("page-1.xhtml", '<img src="pixel.png" alt=""/>'),
            ("page-2.xhtml", '<svg xmlns="http://www.w3.org/2000/svg"><image href="pixel.png"/></svg>'),
        ])
        report = inventory(image_only)
        assert report["readableSpineItems"] == 2
        assert report["spineTextChars"] == 0

        contentless = root / "contentless.epub"
        write_epub(contentless, [("blank.xhtml", ""), ("divider.xhtml", "<hr/>")])
        contentless_output = root / "contentless-output.epub"
        shutil.copyfile(contentless, contentless_output)
        require_pipeline_error("EMPTY_CHAPTERS", lambda: validate({}, contentless, contentless_output, announce=False))

        html_source = root / "repairable.html"
        html_source.write_text("<!doctype html><html><head><meta charset='utf-8'><title>Repair fixture</title></head><body><h1>Sonhos Proibidos</h1>" + "<p>Texto português real para confirmar uma reparação completa e sem perda de conteúdo.</p>" * 30 + "</body></html>", encoding="utf-8")
        clean = root / "clean.epub"
        subprocess.run(["ebook-convert", str(html_source), str(clean), "--output-profile", "kindle_pw3"], check=True, stdout=subprocess.DEVNULL)

        malformed = root / "sonhos-proibidos-malformed.epub"
        def break_epub(name, data):
            if name.lower().endswith(".opf"):
                text = data.decode("utf-8")
                text = re.sub(r"<dc:identifier\b[^>]*>.*?</dc:identifier>", "", text, count=1, flags=re.DOTALL)
                return text.encode()
            if name.lower().endswith((".xhtml", ".html")):
                text = data.decode("utf-8").replace("<html ", '<html id="invalid-root-id" ', 1)
                return text.encode()
            return data
        rewrite_epub(clean, malformed, break_epub)
        require_pipeline_error("EPUBCHECK_FAILED", lambda: epubcheck(malformed))

        repaired = root / "sonhos-proibidos-repaired.epub"
        subprocess.run(["ebook-convert", str(malformed), str(repaired), "--output-profile", "kindle_pw3", "--change-justification", "original"], check=True, stdout=subprocess.DEVNULL)
        report = validate({}, malformed, repaired, announce=False)
        assert report["output"]["readableSpineItems"] > 0
        assert report["epubcheck"]["version"] == "5.3.0"
        with zipfile.ZipFile(repaired) as archive:
            for name in archive.namelist():
                if name.lower().endswith((".xhtml", ".html")):
                    text = archive.read(name).decode("utf-8")
                    root = re.search(r"<html\b[^>]*>", text, flags=re.IGNORECASE)
                    assert not root or not re.search(r"\s+id\s*=", root.group(0), flags=re.IGNORECASE)

    print("validator regression fixtures passed")


if __name__ == "__main__":
    main()
