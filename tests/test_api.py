# =============================================================
# APIの自動テスト
# 実行方法:  cd sekipro-app && py -m pytest -v
#
# TestClient はサーバーを起動せずに FastAPI アプリを直接呼べる仕組み。
# 開発モード(DEV_MODE=1)なので X-Dev-User ヘッダーで別人になりすませる。
# =============================================================
import urllib.parse

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def h(name: str) -> dict:
    """「name という名前のユーザーとしてアクセスする」ためのヘッダーを作る"""
    return {"X-Dev-User": urllib.parse.quote(name)}


# ---- 準備: テスト全体で使い回すルームと課題を作る --------------
@pytest.fixture(scope="module")
def room():
    """たろうがルームを作り、はなこが入室した状態を作る"""
    res = client.post("/api/rooms", json={"room_name": "テスト班", "room_pass": "pass1234"}, headers=h("たろう"))
    assert res.status_code == 200
    room_id = res.json()["room_id"]
    res = client.post("/api/rooms/join", json={"room_id": room_id, "room_pass": "pass1234"}, headers=h("はなこ"))
    assert res.status_code == 200
    return room_id


@pytest.fixture()
def task_id(room):
    """たろうが課題を1件登録した状態を作る(テストごとに新しく作る)"""
    res = client.post(
        "/api/tasks",
        json={"room_id": room, "title": "テスト課題", "deadline": "2026-12-31T23:59", "description": "メモ"},
        headers=h("たろう"),
    )
    assert res.status_code == 200
    return res.json()["task_id"]


# ================= ユーザー登録 =================
def test_register_user():
    res = client.post("/api/users", headers=h("たろう"))
    assert res.status_code == 200
    assert res.json()["display_name"] == "たろう"


# ================= ルーム作成・入室 =================
def test_create_room_validation():
    # ルーム名が空 → 400
    res = client.post("/api/rooms", json={"room_name": " ", "room_pass": "pass1234"}, headers=h("たろう"))
    assert res.status_code == 400
    # 合言葉が短い → 400
    res = client.post("/api/rooms", json={"room_name": "班", "room_pass": "abc"}, headers=h("たろう"))
    assert res.status_code == 400


def test_join_room_wrong_pass(room):
    res = client.post("/api/rooms/join", json={"room_id": room, "room_pass": "wrong"}, headers=h("じろう"))
    assert res.status_code == 404  # 合言葉違いは404(コード違いと区別しない)


def test_non_member_cannot_see_tasks(room):
    res = client.get(f"/api/rooms/{room}/tasks", headers=h("部外者"))
    assert res.status_code == 403


# ================= 課題の登録 (F-1) =================
def test_create_task_validation(room):
    # 課題名が空 → 400
    res = client.post("/api/tasks", json={"room_id": room, "title": "", "deadline": "2026-12-31T23:59"}, headers=h("たろう"))
    assert res.status_code == 400
    # 〆切の形式がおかしい → 400
    res = client.post("/api/tasks", json={"room_id": room, "title": "課題", "deadline": "あした"}, headers=h("たろう"))
    assert res.status_code == 400


def test_task_appears_in_list(room, task_id):
    res = client.get(f"/api/rooms/{room}/tasks", headers=h("はなこ"))
    assert res.status_code == 200
    tasks = res.json()["tasks"]
    target = next(t for t in tasks if t["task_id"] == task_id)
    assert target["title"] == "テスト課題"
    assert target["is_mine"] is False       # はなこから見ると自分の課題ではない
    assert target["reactions"] == {"done": 0, "doing": 0, "help": 0}


# ================= リアクション =================
def test_reaction_toggle(room, task_id):
    # 押す → 付く
    client.post(f"/api/tasks/{task_id}/reactions", json={"type": "doing"}, headers=h("はなこ"))
    res = client.get(f"/api/rooms/{room}/tasks", headers=h("はなこ"))
    target = next(t for t in res.json()["tasks"] if t["task_id"] == task_id)
    assert target["reactions"]["doing"] == 1
    assert target["my_reaction"] == "doing"
    # 別の種類を押す → 変わる(1人1リアクション)
    client.post(f"/api/tasks/{task_id}/reactions", json={"type": "done"}, headers=h("はなこ"))
    res = client.get(f"/api/rooms/{room}/tasks", headers=h("はなこ"))
    target = next(t for t in res.json()["tasks"] if t["task_id"] == task_id)
    assert target["reactions"] == {"done": 1, "doing": 0, "help": 0}
    # 同じ種類をもう一度押す → 取り消し
    client.post(f"/api/tasks/{task_id}/reactions", json={"type": "done"}, headers=h("はなこ"))
    res = client.get(f"/api/rooms/{room}/tasks", headers=h("はなこ"))
    target = next(t for t in res.json()["tasks"] if t["task_id"] == task_id)
    assert target["reactions"]["done"] == 0

def test_invalid_reaction_type(task_id):
    res = client.post(f"/api/tasks/{task_id}/reactions", json={"type": "hmm"}, headers=h("たろう"))
    assert res.status_code == 400


# ================= 課題の編集 =================
def test_edit_by_creator(room, task_id):
    res = client.put(
        f"/api/tasks/{task_id}",
        json={"title": "直した課題", "deadline": "2027-01-15T12:00", "description": "更新後"},
        headers=h("たろう"),
    )
    assert res.status_code == 200
    res = client.get(f"/api/tasks/{task_id}", headers=h("たろう"))
    assert res.json()["task"]["title"] == "直した課題"
    assert res.json()["task"]["deadline"] == "2027-01-15T12:00"


def test_edit_by_other_member_forbidden(task_id):
    res = client.put(
        f"/api/tasks/{task_id}",
        json={"title": "勝手に変更", "deadline": "2027-01-15T12:00"},
        headers=h("はなこ"),
    )
    assert res.status_code == 403  # 登録した本人以外は編集できない


# ================= 課題の削除 =================
def test_delete_by_other_member_forbidden(task_id):
    res = client.delete(f"/api/tasks/{task_id}", headers=h("はなこ"))
    assert res.status_code == 403


def test_delete_by_creator(room, task_id):
    # リアクションが付いていても丸ごと消えることを確認
    client.post(f"/api/tasks/{task_id}/reactions", json={"type": "help"}, headers=h("はなこ"))
    res = client.delete(f"/api/tasks/{task_id}", headers=h("たろう"))
    assert res.status_code == 200
    res = client.get(f"/api/tasks/{task_id}", headers=h("たろう"))
    assert res.status_code == 404  # もう存在しない


# ================= 分担 (UC3) =================
def test_assignment_flow(room, task_id):
    # はなこが「問1〜3」を担当
    res = client.post(f"/api/tasks/{task_id}/assignment", json={"part": "問1〜3"}, headers=h("はなこ"))
    assert res.status_code == 200
    # たろうも担当(範囲メモなし)
    client.post(f"/api/tasks/{task_id}/assignment", json={}, headers=h("たろう"))
    res = client.get(f"/api/tasks/{task_id}", headers=h("はなこ"))
    data = res.json()
    assert [a["name"] for a in data["assignees"]] == ["はなこ", "たろう"]
    assert data["my_assignment"] == {"part": "問1〜3"}
    # 一覧にも出る
    res = client.get(f"/api/rooms/{room}/tasks", headers=h("はなこ"))
    target = next(t for t in res.json()["tasks"] if t["task_id"] == task_id)
    assert len(target["assignees"]) == 2
    # はなこが担当をやめる
    res = client.delete(f"/api/tasks/{task_id}/assignment", headers=h("はなこ"))
    assert res.status_code == 200
    res = client.get(f"/api/tasks/{task_id}", headers=h("はなこ"))
    assert [a["name"] for a in res.json()["assignees"]] == ["たろう"]
    assert res.json()["my_assignment"] is None


def test_assignment_part_too_long(task_id):
    res = client.post(f"/api/tasks/{task_id}/assignment", json={"part": "あ" * 51}, headers=h("たろう"))
    assert res.status_code == 400


# ================= 提出物 =================
def test_submission_validation(task_id):
    # リンクもコメントも空 → 400
    res = client.post(f"/api/tasks/{task_id}/submissions", json={}, headers=h("たろう"))
    assert res.status_code == 400
    # httpで始まらないリンク → 400
    res = client.post(f"/api/tasks/{task_id}/submissions", json={"file_url": "ftp://x"}, headers=h("たろう"))
    assert res.status_code == 400


def test_submission_flow(room, task_id):
    res = client.post(
        f"/api/tasks/{task_id}/submissions",
        json={"file_url": "https://example.com/report.pdf", "comment": "できた"},
        headers=h("はなこ"),
    )
    assert res.status_code == 200
    res = client.get(f"/api/tasks/{task_id}", headers=h("たろう"))
    subs = res.json()["submissions"]
    assert len(subs) == 1
    assert subs[0]["display_name"] == "はなこ"
    assert subs[0]["file_url"] == "https://example.com/report.pdf"
