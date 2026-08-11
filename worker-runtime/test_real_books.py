#!/usr/bin/env python3
"""No-delivery regression gate for explicitly selected production books."""
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, "/app")
import server


ALLOWED_REPAIR_ERRORS = {
    "INVALID_EPUB",
    "BROKEN_MANIFEST",
    "EPUBCHECK_FAILED",
    "EMPTY_CHAPTERS",
    "ENCODING_CORRUPTION",
    "OUTPUT_ENCODING",
}


def verify(slot, incoming):
    with tempfile.TemporaryDirectory(prefix=f"real-book-{slot}-") as folder:
        work = Path(folder)
        source = work / "source.epub"
        output = work / "book.epub"
        shutil.copyfile(incoming, source)
        server.protected(source)
        server.scan(source)
        shutil.copyfile(source, output)
        try:
            report = server.validate({}, source, output, announce=False)
        except server.PipelineError as error:
            if error.code not in ALLOWED_REPAIR_ERRORS:
                raise
            output.unlink(missing_ok=True)
            report = server.convert_and_validate({}, source, output, work)
        if output.stat().st_size > 24 * 1024 * 1024:
            optimized = work / "book-optimized.epub"
            server.convert({}, output, optimized, 85, "VERIFY")
            report = server.validate({}, output, optimized, announce=False)
            output = optimized
        if output.stat().st_size > 24 * 1024 * 1024:
            raise server.PipelineError("TOO_LARGE", "Validated EPUB exceeds the delivery limit")
        summary, encoded = server.validation_summary(report)
        assert zipfile.is_zipfile(output)
        assert summary["output"]["readableSpineItems"] > 0
        assert summary["epubcheck"]["messages"] == 0
        assert len(encoded.encode("ascii")) <= 4096
        print(
            f"real-book-slot-{slot}: PASS "
            f"spine={summary['output']['spineCount']} "
            f"text={summary['output']['textChars']} "
            f"bytes={output.stat().st_size}"
        )


def main():
    # Deliberately disable every network callback. If production code attempts
    # status, upload, or submission during this gate, fail immediately.
    def no_network(*_args, **_kwargs):
        raise AssertionError("A no-delivery real-book gate attempted a network callback")

    server.status = lambda *_args, **_kwargs: None
    server.api = no_network
    failures = []
    for slot, value in enumerate(sys.argv[1:], start=1):
        try:
            verify(slot, Path(value))
        except Exception as error:
            code = error.code if isinstance(error, server.PipelineError) else type(error).__name__
            diagnostic = error.diagnostic if isinstance(error, server.PipelineError) else str(error)
            print(f"real-book-slot-{slot}: FAIL code={code} diagnostic={diagnostic or 'none'}")
            failures.append((slot, code))
    if failures:
        summary = ", ".join(f"slot-{slot}:{code}" for slot, code in failures)
        raise SystemExit(f"Real-book gate failed: {summary}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Expected exactly two private EPUB paths")
    main()
