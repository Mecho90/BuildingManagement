from __future__ import annotations

def theme(request):
    return {
        "theme": request.session.get("theme", "light"),
    }
