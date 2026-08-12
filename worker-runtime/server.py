#!/usr/bin/env python3
"""Isolated, single-book Calibre worker for Os Meus Livros."""
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote, unquote, urlsplit
from urllib.error import HTTPError, URLError
import hashlib, html, json, os, posixpath, re, shutil, subprocess, tempfile, time, unicodedata, zipfile
import xml.etree.ElementTree as ET

APP_ORIGIN = os.environ.get("APP_ORIGIN", "").rstrip("/")
OIDC_REQUEST_URL = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
OIDC_REQUEST_TOKEN = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
MAX_SOURCE = int(os.environ.get("MAX_SOURCE_BYTES", str(50 * 1024 * 1024)))
MAX_UNPACKED = int(os.environ.get("MAX_UNPACKED_BYTES", str(500 * 1024 * 1024)))
MAX_ARCHIVE_FILES = int(os.environ.get("MAX_ARCHIVE_FILES", "10000"))
CALIBRE = os.environ.get("CALIBRE_VERSION", "9.12.0")
EPUBCHECK = os.environ.get("EPUBCHECK_VERSION", "5.3.0")
PROFILE = os.environ.get("PROFILE_VERSION", "default-1")
VALIDATOR = os.environ.get("VALIDATOR_VERSION", "1")
MAX_JOB_AGE = int(os.environ.get("MAX_JOB_AGE_SECONDS", "1680"))
STAGES = {"DOWNLOAD", "CONVERT", "VERIFY", "PREPARE", "SEND"}

class PipelineError(Exception):
    def __init__(self, code, message, diagnostic=None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic

_oidc_cache = {"token": "", "exp": 0, "audience": ""}

def _jwt_exp(token):
    import base64
    encoded = token.split(".")[1]
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    return int(json.loads(base64.urlsafe_b64decode(encoded))["exp"])

def github_identity(subject):
    audience = f"os-meus-livros:{subject}"
    if _oidc_cache["token"] and _oidc_cache["audience"] == audience and _oidc_cache["exp"] > time.time() + 60:
        return _oidc_cache["token"]
    if not OIDC_REQUEST_URL or not OIDC_REQUEST_TOKEN:
        raise PipelineError("WORKER_IDENTITY_UNAVAILABLE", "GitHub job identity is unavailable")
    separator = "&" if "?" in OIDC_REQUEST_URL else "?"
    request = Request(f"{OIDC_REQUEST_URL}{separator}audience={quote(audience, safe='')}", headers={"authorization": f"Bearer {OIDC_REQUEST_TOKEN}"})
    try:
        with urlopen(request, timeout=30) as response:
            token = json.loads(response.read())["value"]
    except (HTTPError, URLError, KeyError, ValueError) as error:
        raise PipelineError("WORKER_IDENTITY_UNAVAILABLE", "GitHub job identity could not be issued") from error
    _oidc_cache.update(token=token, exp=_jwt_exp(token), audience=audience)
    return token

def headers_for(job):
    return {"x-github-oidc-token": github_identity(job["jobId"])}

def api(job, suffix, method="GET", body=None, extra=None, timeout=300):
    url = f'{APP_ORIGIN}/api/internal/jobs/{job["jobId"]}/{suffix}'
    headers = headers_for(job); headers.update(extra or {})
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    if body is not None and not isinstance(body, bytes): headers["content-type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=timeout) as response:
            raw = response.read(); return json.loads(raw) if raw else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise PipelineError("CALLBACK_FAILED", f"{suffix}: {error.code} {detail}")
    except (URLError, TimeoutError) as error:
        raise PipelineError("CALLBACK_FAILED", f"{suffix}: transient network failure") from error

def status(job, stage, detail, state="RUNNING", **extra):
    assert stage in STAGES or stage == "SUBMITTED"
    payload = {"stage": stage, "detail": detail, "state": state}; payload.update(extra)
    # Status writes are idempotent and may be retried safely. A transient proxy
    # stall must not discard a valid conversion after several minutes of work.
    for attempt in range(3):
        try:
            return api(job, "status", "PATCH", payload, timeout=30)
        except PipelineError as error:
            if error.code != "CALLBACK_FAILED" or attempt == 2:
                raise
            time.sleep(1 << attempt)

def ensure_current(job, phase):
    if time.time() - int(job["issuedAt"]) > MAX_JOB_AGE:
        raise PipelineError("WORKER_TIMEOUT", f"Job expired before {phase}")

def safe_zip(path):
    if not zipfile.is_zipfile(path): return
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > MAX_ARCHIVE_FILES: raise PipelineError("ARCHIVE_FILE_LIMIT", "Archive contains too many files")
        total = 0
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts: raise PipelineError("ARCHIVE_TRAVERSAL", "Unsafe archive path")
            total += info.file_size
            if total > MAX_UNPACKED or (info.compress_size and info.file_size / max(1, info.compress_size) > 200): raise PipelineError("DECOMPRESSION_BOMB", "Unsafe archive expansion")

def download(job, target):
    status(job, "DOWNLOAD", "A ligar ao Google Drive")
    request = Request(f'{APP_ORIGIN}/api/internal/jobs/{job["jobId"]}/source', headers=headers_for(job))
    sha = hashlib.sha256(); done = 0
    with urlopen(request, timeout=300) as response, open(target, "wb") as output:
        total = int(response.headers.get("content-length", 0)); disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
        source_name = unquote(match.group(1)) if match else "source.bin"
        last_report = time.monotonic(); last_reported_bytes = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk: break
            done += len(chunk)
            if done > MAX_SOURCE: raise PipelineError("SOURCE_TOO_LARGE", "Source exceeds configured limit")
            output.write(chunk); sha.update(chunk)
            now = time.monotonic()
            if done - last_reported_bytes >= 5 * 1024 * 1024 or now - last_report >= 2:
                status(job, "DOWNLOAD", f"{done} de {total} bytes" if total else f"{done} bytes")
                last_report = now; last_reported_bytes = done
    if done == 0 or (total and done != total): raise PipelineError("TRUNCATED_SOURCE", "Source is empty or truncated")
    if done != last_reported_bytes: status(job, "DOWNLOAD", f"{done} de {total} bytes" if total else f"{done} bytes")
    safe_zip(target)
    return sha.hexdigest(), done, source_name

def scan(path):
    result = subprocess.run(["clamscan", "--no-summary", str(path)], capture_output=True, text=True, timeout=120)
    if result.returncode == 1: raise PipelineError("MALWARE_DETECTED", "Malware scanner rejected source")
    if result.returncode > 1: raise PipelineError("MALWARE_SCAN_ERROR", result.stderr[-500:])

def protected(path):
    suffix = path.suffix.lower()
    if suffix in {".azw", ".azw3", ".mobi"}:
        data = path.read_bytes()[:256]
        if b"BOOKMOBI" in data:
            mobi = data.find(b"MOBI")
            if mobi > 0 and len(data) > mobi + 16:
                # PalmDOC encryption type is immediately before MOBI header.
                enc = int.from_bytes(data[max(0, mobi - 4):max(0, mobi - 2)], "big")
                if enc not in (0,): raise PipelineError("PROTECTED_BOOK", "Encrypted MOBI/AZW input")
    if suffix == ".epub" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if "META-INF/rights.xml" in archive.namelist(): raise PipelineError("PROTECTED_BOOK", "EPUB rights file detected")
            if "META-INF/encryption.xml" in archive.namelist():
                text = archive.read("META-INF/encryption.xml").decode("utf-8", "replace")
                allowed = ("http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC")
                algorithms = re.findall(r'Algorithm=["\']([^"\']+)', text)
                if "EncryptedData" in text and (not algorithms or any(uri not in allowed for uri in algorithms)): raise PipelineError("PROTECTED_BOOK", "Unsupported EPUB encryption")

def encoding_args(path):
    if path.suffix.lower() not in {".txt", ".html", ".htm", ".rtf"}: return []
    sample = path.read_bytes()[:200000]
    try: sample.decode("utf-8"); return ["--input-encoding", "utf-8"]
    except UnicodeDecodeError: return ["--input-encoding", "windows-1252"]

def convert(job, source, output, quality=None, progress_stage="CONVERT"):
    command = ["timeout", "--signal=TERM", "20m", "ebook-convert", str(source), str(output), "--output-profile", "kindle_pw3", "--epub-version", "3", "--change-justification", "original", *encoding_args(source)]
    if quality: command += ["--jpeg-quality", str(quality)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    milestones = [("InputFormatPlugin", "A ler o ficheiro original"), ("Parsing all content", "A construir a estrutura do livro"), ("Processing images", "A processar imagens e estilos"), ("Creating EPUB Output", "A criar o novo EPUB"), ("Output saved", "A finalizar o EPUB")]
    for line in iter(process.stdout.readline, ""):
        for marker, detail in milestones:
            if marker.lower() in line.lower(): status(job, progress_stage, detail)
    if process.wait() != 0 or not output.exists(): raise PipelineError("CALIBRE_FAILED", "Complete Calibre conversion failed")

def opf_path(archive):
    root = ET.fromstring(archive.read("META-INF/container.xml")); node = root.find(".//{*}rootfile")
    if node is None: raise PipelineError("INVALID_EPUB", "Missing package document")
    return node.attrib["full-path"]

def document_content(raw):
    """Return visible body text and visual elements from one HTML spine item."""
    body = re.search(r"<body\b[^>]*>(.*?)</body\s*>", raw, flags=re.IGNORECASE | re.DOTALL)
    content = body.group(1) if body else raw
    content = re.sub(r"<!--.*?-->", " ", content, flags=re.DOTALL)
    content = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", " ", content, flags=re.IGNORECASE | re.DOTALL)
    visuals = len(re.findall(r"<(?:img|image|svg|canvas|video|object)\b", content, flags=re.IGNORECASE))
    plain = html.unescape(re.sub(r"<[^>]+>", " ", content))
    plain = re.sub(r"\s+", " ", plain.replace("\xa0", " ")).strip()
    return plain, visuals

def manifest_resource_name(package, href, names):
    """Resolve an OPF manifest IRI to its case-sensitive ZIP member name.

    EPUB manifest hrefs are IRIs, not literal ZIP paths: percent escapes and URL
    fragments must be resolved before looking in the archive. Some authoring
    tools also write canonically equivalent decomposed Unicode ZIP names. Match
    those without accepting case changes or paths outside the archive root.
    """
    raw_path = unquote(urlsplit(href).path)
    if not raw_path or raw_path.startswith("/"):
        raise PipelineError("BROKEN_MANIFEST", "Manifest contains an empty or absolute resource path", "invalid-manifest-path")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(package), raw_path))
    if resolved == ".." or resolved.startswith("../"):
        raise PipelineError("BROKEN_MANIFEST", "Manifest resource escapes the EPUB root", "escaping-manifest-path")
    if resolved in names:
        return resolved
    normalized = unicodedata.normalize("NFC", resolved)
    matches = [name for name in names if unicodedata.normalize("NFC", name) == normalized]
    if len(matches) == 1:
        return matches[0]
    reason = "ambiguous-unicode-manifest-path" if matches else "missing-manifest-resource"
    raise PipelineError("BROKEN_MANIFEST", "Manifest resource is missing from the EPUB archive", reason)

def inventory(path):
    safe_zip(path)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "mimetype" not in names or archive.read("mimetype") != b"application/epub+zip": raise PipelineError("INVALID_EPUB", "Invalid EPUB mimetype")
        package = opf_path(archive); opf = ET.fromstring(archive.read(package))
        manifest = {item.attrib.get("id"): item for item in opf.findall(".//{*}manifest/{*}item")}
        spine = [item.attrib.get("idref") for item in opf.findall(".//{*}spine/{*}itemref")]
        chapters, resources, texts, empty = [], [], [], []
        readable_spine_items = 0
        spine_text_chars = 0
        spine_visual_items = 0
        for ident, item in manifest.items():
            href = manifest_resource_name(package, item.attrib.get("href", ""), names)
            media = item.attrib.get("media-type", "")
            resources.append((href, media))
            if media in {"application/xhtml+xml", "text/html"}:
                raw = archive.read(href).decode("utf-8", "replace")
                plain, visuals = document_content(raw)
                texts.append(plain)
                if ident in spine:
                    chapters.append(href)
                    spine_text_chars += len(plain)
                    spine_visual_items += int(visuals > 0)
                    if re.search(r"\w", plain, flags=re.UNICODE) or visuals:
                        readable_spine_items += 1
                    else:
                        empty.append(href)
        joined = "\n".join(texts); suspicious = sum(joined.count(x) for x in ("�", "Ã£", "Ã©", "Ã§", "â€œ", "â€™"))
        return {"package": package, "manifestCount": len(manifest), "spineCount": len(spine), "chapters": chapters, "resources": resources, "textChars": len(joined), "paragraphs": len(re.findall(r"\n|[.!?]\s", joined)), "emptyChapters": empty, "readableSpineItems": readable_spine_items, "spineTextChars": spine_text_chars, "spineVisualItems": spine_visual_items, "suspiciousEncoding": suspicious, "replacementCharacters": joined.count("�")}

def source_inventory(path):
    """Return comparison evidence only when the original EPUB is trustworthy.

    The converted output is always normalized and validated strictly. The
    original file is optional evidence used only for text/chapter retention
    comparisons. Real-world EPUBs can be readable by Calibre while carrying a
    malformed mimetype entry, package document, or manifest; those defects must
    trigger full conversion, not crash validation of the repaired output.
    """
    try:
        return inventory(path)
    except PipelineError as error:
        if error.code in {"INVALID_EPUB", "BROKEN_MANIFEST"}:
            return None
        raise
    except (ET.ParseError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return None

def remove_forbidden_root_id(text):
    """Remove an XHTML root id that EPUBCheck rejects, preserving all child ids."""
    root = re.search(r"<(?:[\w.-]+:)?html\b[^>]*>", text, flags=re.IGNORECASE)
    if not root: return text
    cleaned = re.sub(r"\s+id\s*=\s*(?:\"[^\"]*\"|'[^']*')", "", root.group(0), count=1, flags=re.IGNORECASE)
    return text[:root.start()] + cleaned + text[root.end():]

def local_reference_exists(document, reference, names):
    """Return whether an internal document reference resolves in the archive."""
    parsed = urlsplit(html.unescape(reference.strip()))
    if parsed.scheme or parsed.netloc or not parsed.path:
        return True
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document), unquote(parsed.path)))
    normalized = unicodedata.normalize("NFC", resolved)
    return any(unicodedata.normalize("NFC", name) == normalized for name in names)

def normalize_content_document(text, document, names):
    """Repair bounded, content-preserving XHTML conformance defects."""
    def script_block(match):
        tag = match.group(0)
        reference = re.search(r"<script\b[^>]*\ssrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, flags=re.IGNORECASE)
        if reference and not local_reference_exists(document, reference.group(1) or reference.group(2), names):
            return ""
        return tag

    # Kobo and other vendor EPUBs sometimes retain loader tags after the
    # referenced JavaScript file has been omitted. A missing script cannot run
    # or contribute visible book content, but EPUBCheck correctly rejects the
    # broken reference and then also requires a misleading scripted manifest
    # property. Remove only script elements whose local source is absent.
    text = re.sub(r"<script\b[^>]*>.*?</script\s*>", script_block, text, flags=re.IGNORECASE | re.DOTALL)

    def media_tag(match):
        tag = match.group(0)
        reference = re.search(r"\s+(?:src|href|xlink:href)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, flags=re.IGNORECASE)
        if reference and not local_reference_exists(document, reference.group(1) or reference.group(2), names):
            alt = re.search(r"\s+alt\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, flags=re.IGNORECASE)
            alt_text = (alt.group(1) if alt.group(1) is not None else alt.group(2)) if alt else ""
            return html.escape(alt_text or "")
        tag = re.sub(r"\s+(?:width|height)\s*=\s*(?:\"(?!\d+\")[^\"]*\"|'(?!\d+')[^']*')", "", tag, flags=re.IGNORECASE)
        # EPUB XHTML requires alt on HTML img elements. An empty value is the
        # standards-correct representation for a decorative image and changes
        # neither the image nor the book's visible text.
        if re.match(r"<img\b", tag, flags=re.IGNORECASE) and not re.search(r"\s+alt\s*=", tag, flags=re.IGNORECASE):
            tag = re.sub(r"\s*/?>$", lambda ending: f' alt=""{ending.group(0)}', tag, count=1)
        return tag

    text = re.sub(r"<(?:img|(?:[\w.-]+:)?image)\b[^>]*?/?>", media_tag, text, flags=re.IGNORECASE)

    def link_tag(match):
        tag = match.group(0)
        reference = re.search(r"\s+href\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, flags=re.IGNORECASE)
        if reference and not local_reference_exists(document, reference.group(1) or reference.group(2), names):
            return ""
        return tag

    text = re.sub(r"<link\b[^>]*?/?>", link_tag, text, flags=re.IGNORECASE)

    def anchor_tag(match):
        tag = match.group(0)
        reference = re.search(r"\s+href\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, flags=re.IGNORECASE)
        if reference and not local_reference_exists(document, reference.group(1) or reference.group(2), names):
            return re.sub(r"\s+href\s*=\s*(?:\"[^\"]*\"|'[^']*')", "", tag, count=1, flags=re.IGNORECASE)
        return tag

    text = re.sub(r"<a\b[^>]*>", anchor_tag, text, flags=re.IGNORECASE)
    return normalize_inline_block_nesting(text)

def normalize_scripted_manifest(text, package, scripted_documents):
    """Make each XHTML manifest item's scripted property match its content."""
    def manifest_item(match):
        tag = match.group(0)
        href_match = re.search(r"\shref\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, flags=re.IGNORECASE)
        if not href_match:
            return tag
        raw_path = unquote(urlsplit(href_match.group(1) or href_match.group(2)).path)
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(package), raw_path))
        normalized = unicodedata.normalize("NFC", resolved)
        is_scripted = any(unicodedata.normalize("NFC", name) == normalized for name in scripted_documents)
        properties = re.search(r"\sproperties\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, flags=re.IGNORECASE)
        tokens = (properties.group(1) if properties and properties.group(1) is not None else properties.group(2) if properties else "").split()
        tokens = [token for token in tokens if token != "scripted"]
        if is_scripted:
            tokens.append("scripted")
        if properties:
            replacement = f' properties="{" ".join(tokens)}"' if tokens else ""
            return tag[:properties.start()] + replacement + tag[properties.end():]
        if not tokens:
            return tag
        return re.sub(r"\s*/?>$", lambda ending: f' properties="scripted"{ending.group(0)}', tag, count=1)

    return re.sub(r"<(?:[\w.-]+:)?item\b[^>]*?/?>", manifest_item, text, flags=re.IGNORECASE)

def normalize_inline_block_nesting(text):
    """Replace only block tags illegally nested in phrasing-only elements.

    Some source EPUBs contain schema-invalid constructs such as
    ``<p><h1>...</h1></p>``. Calibre preserves the visible content but also
    preserves that invalid nesting across conversion passes. Re-tagging the
    inner block as a span retains its text, attributes, order, and styling while
    making the XHTML content model valid. Valid top-level headings are untouched.
    """
    phrasing_only = {
        "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del",
        "dfn", "em", "h1", "h2", "h3", "h4", "h5", "h6", "i", "ins",
        "kbd", "label", "mark", "p", "q", "s", "samp", "small", "span",
        "strong", "sub", "sup", "time", "u", "var",
    }
    block = {
        "address", "article", "aside", "blockquote", "details", "dialog",
        "dd", "div", "dl", "dt", "fieldset", "figcaption", "figure",
        "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hgroup", "hr", "li", "main", "menu", "nav", "ol",
        "p", "pre", "search", "section", "summary", "table", "ul",
    }

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # The caller still runs EPUBCheck and fails closed; never guess at XML
        # that is not well-formed enough for a structural repair.
        return text

    changed = False
    for parent in root.iter():
        if not isinstance(parent.tag, str):
            continue
        parent_name = parent.tag.rsplit("}", 1)[-1].lower()
        if parent_name not in phrasing_only:
            continue
        for child in list(parent):
            if not isinstance(child.tag, str):
                continue
            child_name = child.tag.rsplit("}", 1)[-1].lower()
            if child_name not in block:
                continue
            namespace = child.tag[:-len(child_name)] if child.tag.startswith("{") else ""
            child.tag = f"{namespace}span"
            changed = True

    if not changed:
        return text

    ET.register_namespace("", "http://www.w3.org/1999/xhtml")
    ET.register_namespace("epub", "http://www.idpf.org/2007/ops")
    rendered = ET.tostring(root, encoding="unicode", method="xml")
    root_tag = re.search(r"<(?:[\w.-]+:)?html\b", text, flags=re.IGNORECASE)
    return (text[:root_tag.start()] if root_tag else "") + rendered

def normalize_epub(path):
    temp = path.with_suffix(".nfc.epub")
    with zipfile.ZipFile(path) as source:
        names = set(source.namelist())
        entries = []
        scripted_documents = set()
        for info in source.infolist():
            if info.filename == "mimetype": continue
            data = source.read(info.filename)
            if info.filename.lower().endswith((".xhtml", ".html", ".htm", ".css", ".opf", ".ncx", ".xml")):
                try:
                    text = unicodedata.normalize("NFC", data.decode("utf-8"))
                    if info.filename.lower().endswith((".xhtml", ".html", ".htm")):
                        text = remove_forbidden_root_id(text)
                        text = normalize_content_document(text, info.filename, names)
                        if re.search(r"<script\b", text, flags=re.IGNORECASE):
                            scripted_documents.add(info.filename)
                    if info.filename.lower().endswith(".opf"):
                        text = re.sub(r"\s+linear\s*=\s*(?:\"no\"|'no')", "", text, flags=re.IGNORECASE)
                    data = text.encode("utf-8")
                except UnicodeDecodeError: raise PipelineError("OUTPUT_ENCODING", f"Non-UTF-8 resource: {info.filename}")
            entries.append((info, data))

        for index, (info, data) in enumerate(entries):
            if info.filename.lower().endswith(".opf"):
                text = data.decode("utf-8")
                data = normalize_scripted_manifest(text, info.filename, scripted_documents).encode("utf-8")
                entries[index] = (info, data)

    with zipfile.ZipFile(temp, "w") as output:
        output.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info, data in entries:
            output.writestr(info, data)
    temp.replace(path)

def epubcheck(path):
    result = subprocess.run(["java", "-jar", "/opt/epubcheck/epubcheck.jar", str(path), "--json", "-"], capture_output=True, text=True, timeout=180)
    try: report = json.loads(result.stdout or "{}")
    except json.JSONDecodeError: report = {"raw": result.stdout[-2000:], "stderr": result.stderr[-1000:]}
    if result.returncode != 0:
        codes = []
        details = []
        for message in report.get("messages", []) if isinstance(report, dict) else []:
            if not isinstance(message, dict): continue
            code = message.get("ID") or message.get("id")
            severity = message.get("severity")
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_-]{1,30}", code):
                token = f"{severity}:{code}" if isinstance(severity, str) else code
                if token not in codes: codes.append(token)
            detail = message.get("message")
            if isinstance(detail, str):
                detail = re.sub(r"\s+", " ", detail).strip()
                detail = re.sub(r"/(?:work|tmp)/[^\s\"']+", "<temporary-path>", detail)
                if detail and detail not in details: details.append(detail)
        diagnostic = "epubcheck:" + (",".join(codes[:12]) if codes else "unclassified")
        if details: diagnostic += " | " + " | ".join(details[:3])
        diagnostic = diagnostic[:450]
        raise PipelineError("EPUBCHECK_FAILED", json.dumps(report)[:1000], diagnostic)
    return report

def validate(job, source, output, announce=True):
    if announce: status(job, "VERIFY", "A verificar a estrutura EPUB")
    normalize_epub(output); current = inventory(output)
    if announce: status(job, "VERIFY", f'{current["spineCount"]} capítulos na ordem de leitura')
    if current["replacementCharacters"] or current["suspiciousEncoding"] > 2: raise PipelineError("ENCODING_CORRUPTION", "Suspicious Portuguese encoding remains")
    # Legal EPUB reading orders may contain covers, image-only pages, section
    # dividers, and intentional blanks. Reject only an entirely contentless
    # reading order; individual blank/support pages are diagnostic information.
    if not current["readableSpineItems"]: raise PipelineError("EMPTY_CHAPTERS", "The reading order contains no readable text or images")
    original = source_inventory(source) if source.suffix.lower() == ".epub" and zipfile.is_zipfile(source) else None
    if original and original["textChars"] > 1000:
        ratio = current["textChars"] / original["textChars"]
        if ratio < .92: raise PipelineError("TEXT_LOSS", f"Output retains only {ratio:.1%} of source text")
        if current["spineCount"] < max(1, original["spineCount"] - 1): raise PipelineError("CHAPTER_LOSS", "Output lost spine chapters")
    if announce: status(job, "VERIFY", "A validar EPUB 3.3 com EPUBCheck")
    standards = epubcheck(output)
    return {"source": original, "output": current, "epubcheck": {"version": EPUBCHECK, "messages": len(standards.get("messages", []))}}

def convert_and_validate(job, source, output, work):
    """Convert once, then do one bounded normalization pass for invalid XML.

    Calibre can preserve malformed embedded markup or stale resource references
    from an input EPUB during its first repair. Re-importing that generated EPUB
    makes Calibre parse its own package and removes those defects. Validation
    compares both passes, so text or chapter loss still fails closed.
    """
    convert(job, source, output)
    try:
        return validate(job, source, output)
    except PipelineError as error:
        if error.code != "EPUBCHECK_FAILED":
            raise
        status(job, "VERIFY", "A normalizar novamente o EPUB reparado")
        normalized = work / "book-normalized.epub"
        convert(job, output, normalized, progress_stage="VERIFY")
        report = validate(job, output, normalized)
        normalized.replace(output)
        return report

def validation_summary(report):
    """Return the small, stable report accepted by the artifact API.

    Full inventories contain every chapter and resource path and can be tens of
    kilobytes long. They must never be truncated into an HTTP header because a
    character slice can produce invalid JSON. Keep only aggregate evidence and
    prove the exact serialized payload is valid and bounded before uploading.
    """
    numeric_fields = (
        "manifestCount", "spineCount", "textChars", "paragraphs",
        "readableSpineItems", "spineTextChars", "spineVisualItems",
        "suspiciousEncoding", "replacementCharacters",
    )

    def compact(inventory):
        if inventory is None:
            return None
        summary = {field: int(inventory.get(field, 0)) for field in numeric_fields}
        summary["blankSpineItems"] = len(inventory.get("emptyChapters", []))
        return summary

    summary = {
        "schemaVersion": 1,
        "source": compact(report.get("source")),
        "output": compact(report.get("output")),
        "epubcheck": {
            "version": str(report.get("epubcheck", {}).get("version", EPUBCHECK)),
            "messages": int(report.get("epubcheck", {}).get("messages", 0)),
        },
    }
    encoded = json.dumps(summary, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("ascii")) > 4096 or json.loads(encoded) != summary:
        raise PipelineError("VALIDATION_SUMMARY_INVALID", "Validation summary could not be serialized safely")
    return summary, encoded

def upload(job, output, report):
    data = output.read_bytes(); digest = hashlib.sha256(data).hexdigest()
    summary, encoded = validation_summary(report)
    headers = {"content-type": "application/epub+zip", "content-length": str(len(data)), "x-content-sha256": digest, "x-validation-summary": encoded}
    api(job, "artifact", "PUT", data, headers); return digest, len(data), summary

def run(job):
    started = time.time(); timings = {}; failure_stage = "DOWNLOAD"
    with tempfile.TemporaryDirectory(prefix="oml-") as folder:
        work = Path(folder); source = work / "source.bin"; output = work / "book.epub"
        try:
            if job.get("origin", "").rstrip("/") != APP_ORIGIN or job.get("calibreVersion") != CALIBRE or job.get("profileVersion") != PROFILE or job.get("validatorVersion") != VALIDATOR: raise PipelineError("CONFIG_MISMATCH", "Worker configuration mismatch")
            t = time.time(); source_hash, source_size, source_name = download(job, source); timings["downloadMs"] = int((time.time()-t)*1000)
            suffix = Path(source_name).suffix.lower(); source_named = work / f"source{suffix or '.bin'}"; source.rename(source_named); source = source_named
            protected(source); scan(source)
            failure_stage = "CONVERT"
            t = time.time()
            if source.suffix.lower() == ".epub":
                status(job, "CONVERT", "A verificar se este EPUB já está pronto", sourceHash=source_hash, sourceSize=source_size)
                shutil.copyfile(source, output)
                try:
                    report = validate(job, source, output, announce=False)
                    status(job, "VERIFY", "EPUB compatível; conversão completa desnecessária")
                except PipelineError as error:
                    if error.code in {"INVALID_EPUB", "BROKEN_MANIFEST", "EPUBCHECK_FAILED", "EMPTY_CHAPTERS", "ENCODING_CORRUPTION", "OUTPUT_ENCODING"}:
                        status(job, "CONVERT", "A reparar o EPUB com Calibre")
                        output.unlink(missing_ok=True)
                        failure_stage = "VERIFY"
                        report = convert_and_validate(job, source, output, work)
                    else:
                        raise
            else:
                status(job, "CONVERT", "A iniciar o Calibre completo", sourceHash=source_hash, sourceSize=source_size)
                failure_stage = "VERIFY"
                report = convert_and_validate(job, source, output, work)
            timings["convertAndVerifyMs"] = int((time.time()-t)*1000)
            failure_stage = "VERIFY"
            if output.stat().st_size > 24 * 1024 * 1024:
                status(job, "VERIFY", "A reduzir imagens para o limite de envio")
                optimized = work / "book-optimized.epub"; convert(job, output, optimized, 85, "VERIFY"); report = validate(job, output, optimized); output = optimized
            if output.stat().st_size > 24 * 1024 * 1024: raise PipelineError("TOO_LARGE", "Validated EPUB exceeds Gmail's attachment limit")
            failure_stage = "PREPARE"
            status(job, "PREPARE", "A confirmar tamanho, nome e metadados")
            ensure_current(job, "artifact upload")
            digest, size, summary = upload(job, output, report); timings["totalMs"] = int((time.time()-started)*1000)
            failure_stage = "SEND"
            if job.get("dryRun"):
                status(job, "SUBMITTED", "Teste concluído sem enviar email nem livro", "SUBMITTED", outputSize=size, validation=summary, timings=timings)
                return
            status(job, "SEND", "A construir uma mensagem com um único EPUB", outputSize=size, validation=summary, timings=timings)
            ensure_current(job, "email submission")
            api(job, "submit", "POST", {"artifactHash": digest})
        except PipelineError as error:
            detail = "O processamento parou em segurança"
            if job.get("dryRun") and error.diagnostic: detail += f" [{error.diagnostic}]"
            try: status(job, failure_stage, detail, "FAILED", errorCode=error.code, timings=timings)
            except Exception: pass
            raise
        except Exception as error:
            try: status(job, failure_stage, "O serviço encontrou um erro inesperado", "FAILED", errorCode="WORKER_INTERNAL_ERROR", timings=timings)
            except Exception: pass
            raise
        finally:
            for child in work.glob("**/*"):
                if child.is_file():
                    try: child.write_bytes(b"\0" * min(child.stat().st_size, 1024 * 1024))
                    except OSError: pass
