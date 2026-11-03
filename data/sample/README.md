# Optional source PDFs

Place original PDFs here using the exact `source_file` filename stored in Qdrant.
The document endpoint checks the user's ordinary permissions before serving a file.
The published chunk snapshot does not include every original PDF.

PDFs are local data and excluded from Git. This directory does not provide an
upload or ingestion feature. The application searches the Qdrant collection,
not files in this folder.
