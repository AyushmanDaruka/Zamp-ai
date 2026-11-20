# Vercel entrypoint — all /api/* requests are rewritten here (see vercel.json).
from app.main import app  # noqa: F401
