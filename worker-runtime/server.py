#!/usr/bin/env python3
"""Isolated, single-book Calibre worker for Os Meus Livros."""
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote, unquote
from urllib.error import HTTPError, URLError
import hashlib, json, os, re, shutil, subprocess, tempfile, time, unicodedata, zipfile
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
    def __init__(self, code, message): super().__init__(message); self.code = code

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

def api(job, suffix, method="GET", body=None, extra=None):
    url = f'{APP_ORIGIN}/api/internal/jobs/{job["jobId"]}/{suffix}'
    headers = headers_for(job); headers.update(extra or {})
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    if body is not None and not isinstance(body, bytes): headers["content-type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=300) as response:
            raw = response.read(); return json.loads(raw) if raw else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise PipelineError("CALLBACK_FAILED", f"{suffix}: {error.code} {detail}")

def status(job, stage, detail, state="RUNNING", **extra):
    assert stage in STAGES or stage == "SUBMITTED"
    payload = {"stage": stage, "detail": detail, "state": state}; payload.update(extra)
    api(job, "status", "PATCH", payload)

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
    command = ["timeout", "--signal=TERM", "20m", "ebook-convert", str(source), str(output), "--output-profile", "kindle_pw3", "--change-justification", "original", *encoding_args(source)]
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

def inventory(path):
    safe_zip(path)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "mimetype" not in names or archive.read("mimetype") != b"application/epub+zip": raise PipelineError("INVALID_EPUB", "Invalid EPUB mimetype")
        package = opf_path(archive); opf = ET.fromstring(archive.read(package)); base = str(Path(package).parent)
        manifest = {item.attrib.get("id"): item for item in opf.findall(".//{*}manifest/{*}item")}
        spine = [item.attrib.get("idref") for item in opf.findall(".//{*}spine/{*}itemref")]
        chapters, resources, texts, empty = [], [], [], []
        for ident, item in manifest.items():
            href = str(Path(base, item.attrib.get("href", ""))).replace("\\", "/").lstrip("./")
            if href not in names: raise PipelineError("BROKEN_MANIFEST", f"Missing resource: {href}")
            media = item.attrib.get("media-type", "")
            resources.append((href, media))
            if media in {"application/xhtml+xml", "text/html"}:
                raw = archive.read(href).decode("utf-8", "replace"); plain = re.sub(r"<[^>]+>", " ", raw); plain = re.sub(r"\s+", " ", plain).strip(); texts.append(plain)
                if ident in spine: chapters.append(href); empty += [href] if len(plain) < 20 else []
        joined = "\n".join(texts); suspicious = sum(joined.count(x) for x in ("�", "Ã£", "Ã©", "Ã§", "â€œ", "â€™"))
        return {"package": package, "manifestCount": len(manifest), "spineCount": len(spine), "chapters": chapters, "resources": resources, "textChars": len(joined), "paragraphs": len(re.findall(r"\n|[.!?]\s", joined)), "emptyChapters": empty, "suspiciousEncoding": suspicious, "replacementCharacters": joined.count("�")}

def normalize_epub(path):
    temp = path.with_suffix(".nfc.epub")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temp, "w") as output:
        output.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info in source.infolist():
            if info.filename == "mimetype": continue
            data = source.read(info.filename)
            if info.filename.lower().endswith((".xhtml", ".html", ".htm", ".css", ".opf", ".ncx", ".xml")):
                try: data = unicodedata.normalize("NFC", data.decode("utf-8")).encode("utf-8")
                except UnicodeDecodeError: raise PipelineError("OUTPUT_ENCODING", f"Non-UTF-8 resource: {info.filename}")
            output.writestr(info, data)
    temp.replace(path)

def epubcheck(path):
    result = subprocess.run(["java", "-jar", "/opt/epubcheck/epubcheck.jar", str(path), "--json", "-"], capture_output=True, text=True, timeout=180)
    try: report = json.loads(result.stdout or "{}")
    except json.JSONDecodeError: report = {"raw": result.stdout[-2000:], "stderr": result.stderr[-1000:]}
    if result.returncode != 0: raise PipelineError("EPUBCHECK_FAILED", json.dumps(report)[:1000])
    return report

def validate(job, source, output, announce=True):
    if announce: status(job, "VERIFY", "A verificar a estrutura EPUB")
    normalize_epub(output); current = inventory(output)
    if announce: status(job, "VERIFY", f'{current["spineCount"]} capítulos na ordem de leitura')
    if current["replacementCharacters"] or current["suspiciousEncoding"] > 2: raise PipelineError("ENCODING_CORRUPTION", "Suspicious Portuguese encoding remains")
    if current["emptyChapters"]: raise PipelineError("EMPTY_CHAPTERS", ", ".join(current["emptyChapters"][:5]))
    original = inventory(source) if source.suffix.lower() == ".epub" and zipfile.is_zipfile(source) else None
    if original and original["textChars"] > 1000:
        ratio = current["textChars"] / original["textChars"]
        if ratio < .92: raise PipelineError("TEXT_LOSS", f"Output retains only {ratio:.1%} of source text")
        if current["spineCount"] < max(1, original["spineCount"] - 1): raise PipelineError("CHAPTER_LOSS", "Output lost spine chapters")
    if announce: status(job, "VERIFY", "A validar EPUB 3.3 com EPUBCheck")
    standards = epubcheck(output)
    return {"source": original, "output": current, "epubcheck": {"version": EPUBCHECK, "messages": len(standards.get("messages", []))}}

def upload(job, output, report):
    data = output.read_bytes(); digest = hashlib.sha256(data).hexdigest()
    headers = {"content-type": "application/epub+zip", "content-length": str(len(data)), "x-content-sha256": digest, "x-validation-summary": json.dumps(report, separators=(",", ":"))[:7000]}
    api(job, "artifact", "PUT", data, headers); return digest, len(data)

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
                        convert(job, source, output)
                        failure_stage = "VERIFY"
                        report = validate(job, source, output)
                    else:
                        raise
            else:
                status(job, "CONVERT", "A iniciar o Calibre completo", sourceHash=source_hash, sourceSize=source_size)
                convert(job, source, output)
                failure_stage = "VERIFY"
                report = validate(job, source, output)
            timings["convertAndVerifyMs"] = int((time.time()-t)*1000)
            failure_stage = "VERIFY"
            if output.stat().st_size > 24 * 1024 * 1024:
                status(job, "VERIFY", "A reduzir imagens para o limite de envio")
                optimized = work / "book-optimized.epub"; convert(job, output, optimized, 85, "VERIFY"); report = validate(job, output, optimized); output = optimized
            if output.stat().st_size > 24 * 1024 * 1024: raise PipelineError("TOO_LARGE", "Validated EPUB exceeds Gmail's attachment limit")
            failure_stage = "PREPARE"
            status(job, "PREPARE", "A confirmar tamanho, nome e metadados")
            ensure_current(job, "artifact upload")
            digest, size = upload(job, output, report); timings["totalMs"] = int((time.time()-started)*1000)
            failure_stage = "SEND"
            status(job, "SEND", "A construir uma mensagem com um único EPUB", outputSize=size, validation=report, timings=timings)
            ensure_current(job, "email submission")
            api(job, "submit", "POST", {"artifactHash": digest})
        except PipelineError as error:
            try: status(job, failure_stage, "O processamento parou em segurança", "FAILED", errorCode=error.code, timings=timings)
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
