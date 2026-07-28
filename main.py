# =============================================================
# セキプロ開発: 課題共有アプリ バックエンド (FastAPI)
#
# 役割:
#   1. static/index.html (LIFFで開く画面) を配信する
#   2. /api/... でルーム・課題・リアクション・提出のAPIを提供する
#   3. 課題が登録されたら LINE Messaging API でメンバーに通知する
#
# 起動方法:  uvicorn main:app --reload
# =============================================================
import hashlib
import hmac
import base64
import os
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()  # .env ファイルから環境変数を読み込む

# ---- 設定 (環境変数) ----------------------------------------
LIFF_ID = os.environ.get("LIFF_ID", "")                     # 例: 1234567890-abcdefgh
LOGIN_CHANNEL_ID = os.environ.get("LINE_LOGIN_CHANNEL_ID", "")   # IDトークン検証に使う
MESSAGING_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")  # Push通知に使う
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")  # Webhook署名検証に使う
DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"           # ローカル開発用(LINEなしで動く)
DB_PATH = os.environ.get("DB_PATH", "sekipro.db")
# 本番(Render)ではNeonのPostgreSQLを使う。設定がなければローカル用のSQLite
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith("postgres")

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row

JST = timezone(timedelta(hours=9))

app = FastAPI(title="セキプロ 課題共有API")


# ---- データベース --------------------------------------------
# SQLite(ローカル)と PostgreSQL(本番)は書き方が少しだけ違う。
# この薄いラッパーが「?」→「%s」の置き換えを吸収して、
# APIのコードはどちらでも同じ書き方で済むようにする。
class Conn:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql: str, params=()):
        if USE_PG:
            sql = sql.replace("?", "%s")  # プレースホルダの方言差を吸収
        return self.raw.execute(sql, params)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def db() -> Conn:
    """DBに接続する。結果はどちらのDBでも row["列名"] で取れる"""
    if USE_PG:
        return Conn(psycopg.connect(DATABASE_URL, row_factory=dict_row))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return Conn(conn)


def init_db():
    """設計書 §4 のスキーマどおりにテーブルを作る(なければ)"""
    # 連番の主キーだけ書き方が違うので、ここで切り替える
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id      TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at   TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS rooms (
            room_id        TEXT PRIMARY KEY,
            room_name      TEXT NOT NULL,
            room_pass_hash TEXT NOT NULL,
            created_by     TEXT NOT NULL REFERENCES users(user_id),
            created_at     TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS room_users (
            id        {pk},
            room_id   TEXT NOT NULL REFERENCES rooms(room_id),
            user_id   TEXT NOT NULL REFERENCES users(user_id),
            joined_at TEXT NOT NULL,
            UNIQUE(room_id, user_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS tasks (
            task_id     {pk},
            room_id     TEXT NOT NULL REFERENCES rooms(room_id),
            created_by  TEXT NOT NULL REFERENCES users(user_id),
            title       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            deadline    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS reactions (
            reaction_id {pk},
            task_id     INTEGER NOT NULL REFERENCES tasks(task_id),
            user_id     TEXT NOT NULL REFERENCES users(user_id),
            type        TEXT NOT NULL CHECK(type IN ('done','doing','help')),
            created_at  TEXT NOT NULL,
            UNIQUE(task_id, user_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS assignments (
            assignment_id {pk},
            task_id       INTEGER NOT NULL REFERENCES tasks(task_id),
            user_id       TEXT NOT NULL REFERENCES users(user_id),
            part          TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            UNIQUE(task_id, user_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS submissions (
            submission_id {pk},
            task_id       INTEGER NOT NULL REFERENCES tasks(task_id),
            user_id       TEXT NOT NULL REFERENCES users(user_id),
            file_url      TEXT NOT NULL DEFAULT '',
            comment       TEXT NOT NULL DEFAULT '',
            submitted_at  TEXT NOT NULL
        )""",
    ]
    conn = db()
    for s in statements:
        conn.execute(s)
    conn.commit()
    conn.close()


init_db()


def now_iso():
    return datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S")


# ---- 合言葉のハッシュ化 ---------------------------------------
def hash_pass(password: str, salt: str | None = None) -> str:
    """pbkdf2でハッシュ化。'salt$ハッシュ' の形で保存する"""
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"


def verify_pass(password: str, stored: str) -> bool:
    salt = stored.split("$")[0]
    return hmac.compare_digest(hash_pass(password, salt), stored)


# ---- 認証: LIFFのIDトークンを検証して本人を特定する -------------
# クライアントが名乗る user_id は信用せず、毎回トークンから取る(設計書 §7)
_token_cache: dict[str, tuple[float, dict]] = {}  # 検証結果を10分キャッシュ


def get_current_user(authorization: str | None, x_dev_user: str | None) -> dict:
    """Authorizationヘッダーのトークンを検証し、{user_id, display_name} を返す"""
    # --- ローカル開発モード: LINEなしでテストできる ---
    if DEV_MODE and x_dev_user:
        name = urllib.parse.unquote(x_dev_user)  # フロントでURLエンコードした名前を戻す
        return {"user_id": f"DEV_{name}", "display_name": name}

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "認証情報がありません")
    id_token = authorization.removeprefix("Bearer ")

    cached = _token_cache.get(id_token)
    if cached and time.time() - cached[0] < 600:
        return cached[1]

    # LINEの検証エンドポイントにトークンを投げて本人確認する
    res = requests.post(
        "https://api.line.me/oauth2/v2.1/verify",
        data={"id_token": id_token, "client_id": LOGIN_CHANNEL_ID},
        timeout=10,
    )
    if res.status_code != 200:
        raise HTTPException(401, "トークンの検証に失敗しました。開き直してください")
    payload = res.json()
    user = {"user_id": payload["sub"], "display_name": payload.get("name", "名無し")}
    _token_cache[id_token] = (time.time(), user)
    return user


def auth_and_upsert(authorization, x_dev_user) -> dict:
    """認証して、usersテーブルに登録(済みなら表示名を更新)して返す"""
    user = get_current_user(authorization, x_dev_user)
    conn = db()
    conn.execute(
        """INSERT INTO users(user_id, display_name, created_at) VALUES(?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name""",
        (user["user_id"], user["display_name"], now_iso()),
    )
    conn.commit()
    conn.close()
    return user


def require_member(conn, room_id: str, user_id: str):
    """そのルームのメンバーかチェック(認可)。違えば403"""
    row = conn.execute(
        "SELECT 1 FROM room_users WHERE room_id=? AND user_id=?", (room_id, user_id)
    ).fetchone()
    if not row:
        raise HTTPException(403, "このルームのメンバーではありません")


# ---- LINE Push通知 -------------------------------------------
def push_to_members(room_id: str, exclude_user: str, text: str):
    """ルームメンバー(登録者本人以外)のLINEに通知を送る。
    通知の失敗で本体の処理(課題登録など)を壊さないよう、全体をtryで包む"""
    try:
        if not MESSAGING_TOKEN:
            # Windowsのコンソールは絵文字を出せないことがあるので安全に出力
            print("[push skip]", text.encode("unicode_escape").decode("ascii")[:120])
            return
        conn = db()
        rows = conn.execute(
            "SELECT user_id FROM room_users WHERE room_id=? AND user_id<>?",
            (room_id, exclude_user),
        ).fetchall()
        conn.close()
        ids = [r["user_id"] for r in rows if not r["user_id"].startswith("DEV_")]
        if not ids:
            return
        requests.post(
            "https://api.line.me/v2/bot/message/multicast",
            headers={"Authorization": f"Bearer {MESSAGING_TOKEN}"},
            json={"to": ids, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
    except Exception as e:
        print("[push error]", repr(e))


# ---- リクエストの型 (Pydanticが自動でバリデーションしてくれる) ----
class RoomCreate(BaseModel):
    room_name: str
    room_pass: str


class RoomJoin(BaseModel):
    room_id: str
    room_pass: str


class TaskCreate(BaseModel):
    room_id: str
    title: str
    deadline: str
    description: str = ""


class TaskUpdate(BaseModel):
    title: str
    deadline: str
    description: str = ""


class ReactionIn(BaseModel):
    type: str  # done / doing / help


class AssignIn(BaseModel):
    part: str = ""  # 担当範囲メモ(任意)


class SubmissionIn(BaseModel):
    file_url: str = ""
    comment: str = ""


# ================= APIエンドポイント =========================
@app.get("/api/config")
def get_config():
    """フロントがLIFF初期化に使う設定を返す"""
    return {"liff_id": LIFF_ID, "dev_mode": DEV_MODE}


@app.post("/api/users")
def register_user(authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """初回アクセス時のユーザー登録(登録済みなら取得)"""
    return auth_and_upsert(authorization, x_dev_user)


@app.get("/api/me/rooms")
def my_rooms(authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    user = auth_and_upsert(authorization, x_dev_user)
    conn = db()
    rows = conn.execute(
        """SELECT r.room_id, r.room_name,
                  (SELECT COUNT(*) FROM room_users m WHERE m.room_id=r.room_id) AS members
           FROM rooms r JOIN room_users ru ON ru.room_id = r.room_id
           WHERE ru.user_id=? ORDER BY ru.joined_at DESC""",
        (user["user_id"],),
    ).fetchall()
    conn.close()
    return {"user": user, "rooms": [dict(r) for r in rows]}


@app.post("/api/rooms")
def create_room(body: RoomCreate, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """ルーム作成 (F-4)。招待コードを自動発行する"""
    user = auth_and_upsert(authorization, x_dev_user)
    if not body.room_name.strip() or len(body.room_name) > 30:
        raise HTTPException(400, "ルーム名は1〜30文字で入力してください")
    if len(body.room_pass) < 4:
        raise HTTPException(400, "合言葉は4文字以上にしてください")

    # 紛らわしい文字(0/O, 1/I)を除いた6文字の招待コード
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    conn = db()
    for _ in range(20):
        code = "".join(secrets.choice(chars) for _ in range(6))
        if not conn.execute("SELECT 1 FROM rooms WHERE room_id=?", (code,)).fetchone():
            break
    else:
        raise HTTPException(500, "正常に作成されませんでした")

    conn.execute(
        "INSERT INTO rooms(room_id, room_name, room_pass_hash, created_by, created_at) VALUES(?,?,?,?,?)",
        (code, body.room_name.strip(), hash_pass(body.room_pass), user["user_id"], now_iso()),
    )
    conn.execute(
        "INSERT INTO room_users(room_id, user_id, joined_at) VALUES(?,?,?)",
        (code, user["user_id"], now_iso()),
    )
    conn.commit()
    conn.close()
    return {"room_id": code, "room_name": body.room_name.strip()}


@app.post("/api/rooms/join")
def join_room(body: RoomJoin, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """ルーム検索・入室 (F-5)"""
    user = auth_and_upsert(authorization, x_dev_user)
    conn = db()
    room = conn.execute(
        "SELECT * FROM rooms WHERE room_id=?", (body.room_id.strip().upper(),)
    ).fetchone()
    # コード違いでも合言葉違いでも同じメッセージにする(どちらが違うか教えない)
    if not room or not verify_pass(body.room_pass, room["room_pass_hash"]):
        conn.close()
        raise HTTPException(404, "roomが見つかりませんでした")
    conn.execute(
        """INSERT INTO room_users(room_id, user_id, joined_at) VALUES(?,?,?)
           ON CONFLICT(room_id, user_id) DO NOTHING""",
        (room["room_id"], user["user_id"], now_iso()),
    )
    conn.commit()
    conn.close()
    return {"room_id": room["room_id"], "room_name": room["room_name"]}


@app.get("/api/rooms/{room_id}/tasks")
def list_tasks(room_id: str, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """課題一覧 (F-2)。〆切の早い順。各課題にリアクション集計を付ける"""
    user = auth_and_upsert(authorization, x_dev_user)
    conn = db()
    require_member(conn, room_id, user["user_id"])
    room = conn.execute("SELECT room_name FROM rooms WHERE room_id=?", (room_id,)).fetchone()
    members = conn.execute(
        """SELECT u.display_name FROM room_users ru JOIN users u ON u.user_id=ru.user_id
           WHERE ru.room_id=? ORDER BY ru.joined_at""",
        (room_id,),
    ).fetchall()
    tasks = conn.execute(
        """SELECT t.*, u.display_name AS created_by_name
           FROM tasks t JOIN users u ON u.user_id = t.created_by
           WHERE t.room_id=? ORDER BY t.deadline ASC""",
        (room_id,),
    ).fetchall()
    result = []
    for t in tasks:
        counts = {"done": 0, "doing": 0, "help": 0}
        for r in conn.execute(
            "SELECT type, COUNT(*) AS c FROM reactions WHERE task_id=? GROUP BY type", (t["task_id"],)
        ):
            counts[r["type"]] = r["c"]
        mine = conn.execute(
            "SELECT type FROM reactions WHERE task_id=? AND user_id=?",
            (t["task_id"], user["user_id"]),
        ).fetchone()
        subs = conn.execute(
            "SELECT COUNT(*) AS c FROM submissions WHERE task_id=?", (t["task_id"],)
        ).fetchone()["c"]
        assignees = conn.execute(
            """SELECT a.user_id, a.part, u.display_name FROM assignments a
               JOIN users u ON u.user_id=a.user_id WHERE a.task_id=? ORDER BY a.assignment_id""",
            (t["task_id"],),
        ).fetchall()
        my_assign = next((a for a in assignees if a["user_id"] == user["user_id"]), None)
        result.append(dict(t) | {
            "reactions": counts,
            "my_reaction": mine["type"] if mine else None,
            "submission_count": subs,
            "assignees": [{"name": a["display_name"], "part": a["part"]} for a in assignees],
            "my_assignment": {"part": my_assign["part"]} if my_assign else None,
            "is_mine": t["created_by"] == user["user_id"],
        })
    conn.close()
    return {
        "room": {"room_id": room_id, "room_name": room["room_name"]},
        "members": [m["display_name"] for m in members],
        "tasks": result,
    }


@app.post("/api/tasks")
def create_task(body: TaskCreate, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """課題の登録 (F-1)。成功したらメンバーにPush通知"""
    user = auth_and_upsert(authorization, x_dev_user)
    if not body.title.strip() or len(body.title) > 100:
        raise HTTPException(400, "課題名は1〜100文字で入力してください")
    try:
        deadline = datetime.fromisoformat(body.deadline)
    except ValueError:
        raise HTTPException(400, "〆切の日時の形式が正しくありません")
    conn = db()
    require_member(conn, body.room_id, user["user_id"])
    # RETURNING で登録した行のIDをそのまま受け取る(SQLite/PostgreSQL共通の書き方)
    cur = conn.execute(
        "INSERT INTO tasks(room_id, created_by, title, description, deadline, created_at) VALUES(?,?,?,?,?,?) RETURNING task_id",
        (body.room_id, user["user_id"], body.title.strip(), body.description.strip(),
         deadline.strftime("%Y-%m-%dT%H:%M"), now_iso()),
    )
    task_id = cur.fetchone()["task_id"]
    conn.commit()
    conn.close()
    push_to_members(
        body.room_id, user["user_id"],
        f"📚 新しい課題が共有されました\n「{body.title.strip()}」\n〆切: {deadline.strftime('%m/%d %H:%M')}\n登録: {user['display_name']}",
    )
    return {"task_id": task_id}


def get_task_or_404(conn, task_id: int):
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "課題が見つかりません")
    return task


@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdate, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """課題の編集。登録した本人だけができる"""
    user = auth_and_upsert(authorization, x_dev_user)
    if not body.title.strip() or len(body.title) > 100:
        raise HTTPException(400, "課題名は1〜100文字で入力してください")
    try:
        deadline = datetime.fromisoformat(body.deadline)
    except ValueError:
        raise HTTPException(400, "〆切の日時の形式が正しくありません")
    conn = db()
    task = get_task_or_404(conn, task_id)
    require_member(conn, task["room_id"], user["user_id"])
    if task["created_by"] != user["user_id"]:
        raise HTTPException(403, "編集できるのは登録した本人だけです")
    conn.execute(
        "UPDATE tasks SET title=?, description=?, deadline=? WHERE task_id=?",
        (body.title.strip(), body.description.strip(), deadline.strftime("%Y-%m-%dT%H:%M"), task_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """課題の削除。登録した本人だけができる。ぶら下がるデータも一緒に消す"""
    user = auth_and_upsert(authorization, x_dev_user)
    conn = db()
    task = get_task_or_404(conn, task_id)
    require_member(conn, task["room_id"], user["user_id"])
    if task["created_by"] != user["user_id"]:
        raise HTTPException(403, "削除できるのは登録した本人だけです")
    # 外部キー制約があるので、子テーブル(リアクション等)から順に消す
    for table in ("reactions", "assignments", "submissions"):
        conn.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/assignment")
def assign_self(task_id: int, body: AssignIn, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """課題の分担 (UC3): 自分を担当者として登録する(登録済みなら担当範囲を更新)"""
    user = auth_and_upsert(authorization, x_dev_user)
    if len(body.part) > 50:
        raise HTTPException(400, "担当範囲は50文字以内で入力してください")
    conn = db()
    task = get_task_or_404(conn, task_id)
    require_member(conn, task["room_id"], user["user_id"])
    conn.execute(
        """INSERT INTO assignments(task_id, user_id, part, created_at) VALUES(?,?,?,?)
           ON CONFLICT(task_id, user_id) DO UPDATE SET part=excluded.part""",
        (task_id, user["user_id"], body.part.strip(), now_iso()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/tasks/{task_id}/assignment")
def unassign_self(task_id: int, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """課題の分担をやめる(自分の担当登録を消す)"""
    user = auth_and_upsert(authorization, x_dev_user)
    conn = db()
    task = get_task_or_404(conn, task_id)
    require_member(conn, task["room_id"], user["user_id"])
    conn.execute(
        "DELETE FROM assignments WHERE task_id=? AND user_id=?", (task_id, user["user_id"])
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/reactions")
def react(task_id: int, body: ReactionIn, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """リアクションの登録・変更。同じtypeをもう一度押したら取り消し"""
    user = auth_and_upsert(authorization, x_dev_user)
    if body.type not in ("done", "doing", "help"):
        raise HTTPException(400, "不正なリアクションです")
    conn = db()
    task = conn.execute("SELECT room_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "課題が見つかりません")
    require_member(conn, task["room_id"], user["user_id"])
    current = conn.execute(
        "SELECT type FROM reactions WHERE task_id=? AND user_id=?", (task_id, user["user_id"])
    ).fetchone()
    if current and current["type"] == body.type:
        conn.execute("DELETE FROM reactions WHERE task_id=? AND user_id=?", (task_id, user["user_id"]))
    else:
        conn.execute(
            """INSERT INTO reactions(task_id, user_id, type, created_at) VALUES(?,?,?,?)
               ON CONFLICT(task_id, user_id) DO UPDATE SET type=excluded.type, created_at=excluded.created_at""",
            (task_id, user["user_id"], body.type, now_iso()),
        )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """課題詳細 + 提出物一覧 (S6用)"""
    user = auth_and_upsert(authorization, x_dev_user)
    conn = db()
    task = conn.execute(
        """SELECT t.*, u.display_name AS created_by_name FROM tasks t
           JOIN users u ON u.user_id=t.created_by WHERE t.task_id=?""",
        (task_id,),
    ).fetchone()
    if not task:
        raise HTTPException(404, "課題が見つかりません")
    require_member(conn, task["room_id"], user["user_id"])
    subs = conn.execute(
        """SELECT s.*, u.display_name FROM submissions s
           JOIN users u ON u.user_id=s.user_id WHERE s.task_id=? ORDER BY s.submitted_at DESC""",
        (task_id,),
    ).fetchall()
    assignees = conn.execute(
        """SELECT a.user_id, a.part, u.display_name FROM assignments a
           JOIN users u ON u.user_id=a.user_id WHERE a.task_id=? ORDER BY a.assignment_id""",
        (task_id,),
    ).fetchall()
    conn.close()
    my_assign = next((a for a in assignees if a["user_id"] == user["user_id"]), None)
    return {
        "task": dict(task) | {"is_mine": task["created_by"] == user["user_id"]},
        "submissions": [dict(s) for s in subs],
        "assignees": [{"name": a["display_name"], "part": a["part"]} for a in assignees],
        "my_assignment": {"part": my_assign["part"]} if my_assign else None,
    }


@app.post("/api/tasks/{task_id}/submissions")
def submit(task_id: int, body: SubmissionIn, authorization: str | None = Header(None), x_dev_user: str | None = Header(None)):
    """課題の提出 (F-3)。OneDrive等の共有リンク+コメントを共有する"""
    user = auth_and_upsert(authorization, x_dev_user)
    if not body.file_url.strip() and not body.comment.strip():
        raise HTTPException(400, "リンクかコメントのどちらかを入力してください")
    if body.file_url and not body.file_url.startswith(("http://", "https://")):
        raise HTTPException(400, "リンクは http(s):// から始まるURLにしてください")
    conn = db()
    task = conn.execute("SELECT room_id, title FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "課題が見つかりません")
    require_member(conn, task["room_id"], user["user_id"])
    conn.execute(
        "INSERT INTO submissions(task_id, user_id, file_url, comment, submitted_at) VALUES(?,?,?,?,?)",
        (task_id, user["user_id"], body.file_url.strip(), body.comment.strip(), now_iso()),
    )
    conn.commit()
    conn.close()
    push_to_members(
        task["room_id"], user["user_id"],
        f"✅ {user['display_name']} さんが「{task['title']}」の提出物を共有しました",
    )
    return {"ok": True}


# ---- LINE Bot Webhook ----------------------------------------
@app.post("/api/webhook")
async def webhook(request: Request, x_line_signature: str | None = Header(None)):
    """LINEプラットフォームからのWebhook。署名を検証して本物か確かめる(設計書 §7)"""
    body = await request.body()
    if CHANNEL_SECRET:
        digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(base64.b64encode(digest).decode(), x_line_signature or ""):
            raise HTTPException(400, "署名が不正です")
    return {"ok": True}


# ---- 画面(静的ファイル)の配信 --------------------------------
@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
