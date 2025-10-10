import os, argparse
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, Form, Request, Path
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from django.contrib.auth.hashers import make_password, check_password
from sqlalchemy import func

from .database import Base, engine, get_db
from .models import User, Task, Category, TASK_STATUSES
from .django_boot import setup_django
from .render import render_template

def create_app() -> FastAPI:
    setup_django()
    app = FastAPI(title="Categories & Tasks")
    app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "replace-me"))

    @app.on_event("startup")
    def _init_db() -> None:
        Base.metadata.create_all(bind=engine)

    def uid(request: Request) -> int | None:
        return request.session.get("user_id")
    
    @app.get("/", include_in_schema=False)
    def home_root() -> RedirectResponse:
        return RedirectResponse(url="/categories", status_code=303)

    # ---------- Auth
    @app.get("/login")
    def get_login(request: Request):
        if uid(request):
            return RedirectResponse(url="/categories", status_code=303)
        return render_template("login.html", {"error": None})

    @app.post("/login")
    def post_login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
        user = db.query(User).filter(User.username == username).first()
        if not user or not check_password(password, user.password_hash):
            return render_template("login.html", {"error": "Invalid username or password."}, status_code=401)
        request.session["user_id"] = user.id
        return RedirectResponse(url="/categories", status_code=303)  # go to Categories

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.post("/signup")
    def signup(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
        if db.query(User).filter(User.username == username).first():
            return RedirectResponse(url="/login", status_code=303)
        db.add(User(username=username, password_hash=make_password(password)))
        db.commit()
        return RedirectResponse(url="/login", status_code=303)

    # ---------- Categories
    @app.get("/categories")
    def categories_page(request: Request, db: Session = Depends(get_db)):
        user_id = uid(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)

        cats = (
            db.query(Category)
            .filter(Category.user_id == user_id)
            .order_by(Category.created_at.desc())
            .all()
        )

        # Attach counts as attributes (why: Django templates can’t index dicts with dynamic keys)
        for c in cats:
            c.active_count = (
                db.query(func.count(Task.id))
                .filter(Task.user_id == user_id, Task.category_id == c.id, Task.archived.is_(False))
                .scalar()
            )
            c.archived_count = (
                db.query(func.count(Task.id))
                .filter(Task.user_id == user_id, Task.category_id == c.id, Task.archived.is_(True))
                .scalar()
            )

        user = db.get(User, user_id)
        return render_template("categories.html", {
            "username": user.username if user else "unknown",
            "categories": cats,
        })

    @app.post("/categories")
    def create_category(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
        user_id = uid(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        name = " ".join(name.split())
        if name:
            db.add(Category(user_id=user_id, name=name))
            db.commit()
        return RedirectResponse(url="/categories", status_code=303)

    @app.get("/categories/{cid}")
    def category_detail(request: Request, cid: int = Path(..., ge=1), db: Session = Depends(get_db)):
        user_id = uid(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        cat = db.query(Category).filter(Category.id == cid, Category.user_id == user_id).first()
        if not cat:
            return RedirectResponse(url="/categories", status_code=303)
        active = (
            db.query(Task)
            .filter(Task.user_id == user_id, Task.category_id == cid, Task.archived.is_(False))
            .order_by(Task.created_at.desc())
            .all()
        )
        archived = (
            db.query(Task)
            .filter(Task.user_id == user_id, Task.category_id == cid, Task.archived.is_(True))
            .order_by(Task.archived_at.desc().nullslast())
            .all()
        )
        return render_template("category_detail.html", {
            "username": db.get(User, user_id).username,
            "category": cat,
            "tasks": active,
            "archived": archived,
            "STATUSES": TASK_STATUSES,
        })

    # ---------- Tasks inside category
    @app.post("/categories/{cid}/tasks")
    def create_task_in_category(request: Request, cid: int = Path(..., ge=1), title: str = Form(...), db: Session = Depends(get_db)):
        user_id = uid(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        cat = db.query(Category).filter(Category.id == cid, Category.user_id == user_id).first()
        if not cat:
            return RedirectResponse(url="/categories", status_code=303)
        title = " ".join(title.split())
        if title:
            db.add(Task(user_id=user_id, category_id=cid, title=title, status="В процел на обработка"))
            db.commit()
        return RedirectResponse(url=f"/categories/{cid}", status_code=303)

    @app.post("/tasks/{task_id}/status")
    def change_status(request: Request, task_id: int = Path(..., ge=1), status: str = Form(...), db: Session = Depends(get_db)):
        user_id = uid(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        status = status.strip()
        if status not in TASK_STATUSES:
            # silently ignore invalid
            # redirect to category detail if possible
            t = db.get(Task, task_id)
            return RedirectResponse(url=f"/categories/{t.category_id if t else ''}", status_code=303)
        t = db.query(Task).filter(Task.id == task_id, Task.user_id == user_id, Task.archived.is_(False)).first()
        if t:
            t.status = status
            db.commit()
            return RedirectResponse(url=f"/categories/{t.category_id}", status_code=303)
        return RedirectResponse(url="/categories", status_code=303)

    @app.post("/tasks/{task_id}/archive")
    def archive_task(request: Request, task_id: int = Path(..., ge=1), db: Session = Depends(get_db)):
        user_id = uid(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        t = db.query(Task).filter(Task.id == task_id, Task.user_id == user_id, Task.archived.is_(False)).first()
        if t and t.status == "Обработено":
            t.archived = True
            t.archived_at = datetime.now(timezone.utc)
            db.commit()
            return RedirectResponse(url=f"/categories/{t.category_id}", status_code=303)
        return RedirectResponse(url="/categories", status_code=303)

    @app.post("/tasks/{task_id}/unarchive")
    def unarchive_task(request: Request, task_id: int = Path(..., ge=1), db: Session = Depends(get_db)):
        user_id = uid(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        t = db.query(Task).filter(Task.id == task_id, Task.user_id == user_id, Task.archived.is_(True)).first()
        if t:
            t.archived = False
            t.archived_at = None
            db.commit()
            return RedirectResponse(url=f"/categories/{t.category_id}", status_code=303)
        return RedirectResponse(url="/categories", status_code=303)

    @app.post("/tasks/{task_id}/delete")
    def delete_task(request: Request, task_id: int = Path(..., ge=1), db: Session = Depends(get_db)):
        user_id = uid(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        t = db.query(Task).filter(Task.id == task_id, Task.user_id == user_id).first()
        if t:
            cid = t.category_id
            db.delete(t)
            db.commit()
            return RedirectResponse(url=f"/categories/{cid}", status_code=303)
        return RedirectResponse(url="/categories", status_code=303)

    return app

app = create_app()

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    p.add_argument("--reload", action="store_true", default=os.getenv("RELOAD", "false").lower() in {"1","true","yes"})
    p.add_argument("--workers", type=int, default=int(os.getenv("WORKERS", "1")))
    return p.parse_args()

def main() -> None:
    import uvicorn
    args = _parse_args()
    uvicorn.run(
        "building_management.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=None if args.reload else args.workers,
        log_level=os.getenv("LOG_LEVEL", "info"),
    )

if __name__ == "__main__":
    main()