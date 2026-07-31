# Kindle conversion worker

This repository contains the isolated conversion worker for **Os Meus Livros**. A GitHub Actions run downloads one explicitly selected book from the private application, converts it with pinned Calibre, validates the resulting EPUB, uploads the verified artifact, and exits.

The repository contains no application credentials, OAuth secrets, Kindle addresses, or book files. Runtime authentication is supplied only through the encrypted `WORKER_SHARED_SECRET` Actions secret.
