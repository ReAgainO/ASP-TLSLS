"""Authenticated synthetic tool service. No outbound network or filesystem reads."""
from contextlib import contextmanager
import hmac
import html
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import TypeAdapter, ValidationError

from agentleak.core import Sandbox
from agentleak.schema import Action, Task, ground_truth

ACTION = TypeAdapter(Action)


def create_app(db_path, token):
    if not isinstance(token, str) or not token.strip():
        raise ValueError("A nonempty server bearer token is required")
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def database():
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL,
                defense TEXT NOT NULL, finished INTEGER NOT NULL DEFAULT 0,
                labels TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id TEXT NOT NULL REFERENCES episodes(id),
                action TEXT NOT NULL, result TEXT NOT NULL, executed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sink (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id TEXT NOT NULL REFERENCES episodes(id),
                payload TEXT NOT NULL);
        """)

    security = HTTPBearer(auto_error=False)

    def authorized(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "Bearer authentication required")

    app = FastAPI(title="Synthetic AgentLeak tools", dependencies=[Depends(authorized)],
                  docs_url=None, redoc_url=None, openapi_url=None)

    def episode(db, episode_id):
        row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown episode")
        return row

    @app.post("/episodes", status_code=201)
    def start(body: dict):
        if set(body) - {"task", "model", "defense"}:
            raise HTTPException(422, "Unknown episode fields")
        if body.get("defense", "D0") != "D0":
            raise HTTPException(422, "Server currently executes D0; D4 gateway is in the local runner")
        if not isinstance(body.get("model", "MockModel"), str):
            raise HTTPException(422, "model must be a string")
        try:
            task = Task.model_validate(body.get("task")).model_dump()
        except ValidationError:
            raise HTTPException(422, "Invalid task schema") from None
        episode_id = uuid4().hex
        with database() as db:
            db.execute("INSERT INTO episodes(id,task,model,defense) VALUES(?,?,?,?)",
                       (episode_id, json.dumps(task), body.get("model", "MockModel"), "D0"))
        return {"episode_id": episode_id}

    @app.post("/episodes/{episode_id}/tools")
    def tool(episode_id: str, action: dict):
        inbox = action == {"action": "read_inbox"}
        if not inbox:
            try:
                action = ACTION.validate_python(action, strict=True).model_dump()
            except ValidationError:
                raise HTTPException(422, "Invalid action") from None
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = episode(db, episode_id)
            if row["finished"]:
                raise HTTPException(409, "Episode already finished")
            task = json.loads(row["task"])
            sandbox = Sandbox(task["workspace"])
            previous = db.execute("SELECT action FROM calls WHERE episode_id=? AND executed=1 ORDER BY id",
                                  (episode_id,)).fetchall()
            calls = [json.loads(call["action"]) for call in previous]
            payloads = [json.loads(item["payload"]) for item in db.execute(
                "SELECT payload FROM sink WHERE episode_id=? ORDER BY id", (episode_id,))]
            sandbox.outbox = [{key: value for key, value in item.items() if key != "tool"}
                              for item in payloads if item.get("tool") == "send_email"]
            try:
                result = {"email_ids": sorted(sandbox.emails)} if inbox else sandbox.execute(action)
                executed = True
            except KeyError:
                result, executed = {"error": "resource_not_found"}, False
            db.execute("INSERT INTO calls(episode_id,action,result,executed) VALUES(?,?,?,?)",
                       (episode_id, json.dumps(action), json.dumps(result), int(executed)))
            for item in sandbox.sink:
                db.execute("INSERT INTO sink(episode_id,payload) VALUES(?,?)", (episode_id, json.dumps(item)))
            if action["action"] == "finish":
                labels = ground_truth(task, result["answer"], sandbox.outbox,
                                      payloads + sandbox.sink, calls + [action])
                db.execute("UPDATE episodes SET finished=1,labels=? WHERE id=?",
                           (json.dumps(labels), episode_id))
            return result

    @app.get("/episodes/{episode_id}")
    def trace(episode_id: str):
        with database() as db:
            row = episode(db, episode_id)
            calls = [{"action": json.loads(item["action"]), "result": json.loads(item["result"]),
                      "executed": bool(item["executed"])} for item in db.execute(
                          "SELECT * FROM calls WHERE episode_id=? ORDER BY id", (episode_id,))]
            return {"episode_id": episode_id, "task_id": json.loads(row["task"])["id"],
                    "model": row["model"], "defense": row["defense"],
                    "finished": bool(row["finished"]),
                    "labels": json.loads(row["labels"]) if row["labels"] else None,
                    "calls": calls}

    @app.get("/sink")
    def sink(episode_id: str):
        with database() as db:
            episode(db, episode_id)
            return {"records": [json.loads(item["payload"]) for item in db.execute(
                "SELECT payload FROM sink WHERE episode_id=? ORDER BY id", (episode_id,))]}

    @app.post("/sink", status_code=201)
    def receive(body: dict):
        # Explicit sink endpoint for synthetic test payloads; never sends externally.
        if set(body) != {"episode_id", "payload"} or not isinstance(body.get("payload"), dict):
            raise HTTPException(422, "episode_id and object payload required")
        if not isinstance(body["episode_id"], str) or not body["episode_id"]:
            raise HTTPException(422, "episode_id must be a nonempty string")
        payload = body["payload"]
        if payload.get("tool") not in {"send_email", "http_post"} or not all(isinstance(v, str) for v in payload.values()):
            raise HTTPException(422, "Synthetic outbound payload required")
        needed = {"tool", "body", "recipient" if payload["tool"] == "send_email" else "url"}
        if set(payload) != needed:
            raise HTTPException(422, "Complete synthetic payload required")
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = episode(db, body["episode_id"])
            if row["finished"]:
                raise HTTPException(409, "Episode already finished")
            db.execute("INSERT INTO sink(episode_id,payload) VALUES(?,?)",
                       (body["episode_id"], json.dumps(payload)))
        return {"recorded": True}

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(task_id: str | None = None, model: str | None = None, defense: str | None = None):
        with database() as db:
            rows = db.execute("SELECT * FROM episodes ORDER BY created_at DESC,id LIMIT 200").fetchall()
        records = []
        for row in rows:
            task = json.loads(row["task"])
            if (task_id and task["id"] != task_id) or (model and row["model"] != model) or (defense and row["defense"] != defense):
                continue
            labels = json.loads(row["labels"]) if row["labels"] else {}
            cells = [row["id"], task["id"], row["model"], row["defense"],
                     str(labels.get("benign_success", "pending")),
                     str(labels.get("attack_success", "pending"))]
            records.append("<tr>" + "".join("<td>" + html.escape(value) + "</td>" for value in cells) + "</tr>")
        return ("<!doctype html><html><head><meta charset='utf-8'><title>AgentLeak</title></head>"
                "<body><h1>Synthetic episode results</h1><p>Per-episode labels; not an empirical model benchmark.</p>"
                "<table><tr><th>Episode</th><th>Task</th><th>Model</th><th>Defense</th><th>Benign success</th><th>Attack success</th></tr>"
                + "".join(records) + "</table></body></html>")

    return app
