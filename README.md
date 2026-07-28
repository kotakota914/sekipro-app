# 課題共有アプリ(セキプロ開発)

LINEの中で動く、グループの課題共有アプリ。
誰か1人が課題をメモすれば、グループ全員のLINEに通知が届く。

- フロントエンド: LIFF + HTML/JS(`static/index.html`)
- バックエンド: Python / FastAPI(`main.py`)
- DB: SQLite(自動で `sekipro.db` が作られる)

## 機能

- **課題の登録・共有**: 課題名・〆切・メモを登録すると、同じルームの全員に表示&LINE通知
- **課題一覧**: 〆切が近い順に表示。48時間以内は「急いで!」、過ぎたものは「期限切れ」表示
- **編集・削除**: 登録した本人だけが課題を直したり消したりできる
- **リアクション**: ✅終わった / ✍️やってる / 🆘助けて をワンタップで共有
- **分担 (UC3)**: 「担当する」ボタンで自分を担当者に登録。担当範囲メモも書ける
- **提出物の共有**: OneDrive等のリンク+コメントをルーム内に共有

## ローカルで動かす(LINE不要の開発モード)

```powershell
cd sekipro-app
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # DEV_MODE=1 のままでOK
uvicorn main:app --reload
```

http://localhost:8000 を開くとテスト用の名前を聞かれ、LINEなしで全機能を試せる。
APIの仕様書は http://localhost:8000/docs に自動生成される。

## テスト

```powershell
pip install -r requirements-dev.txt
python -m pytest -v
```

`tests/test_api.py` が全APIの正常系・異常系(権限チェック含む)を自動で確認する。

## 公開して LIFF に貼るURLを作る(Neon + Render)

DBはローカルではSQLite、本番では `DATABASE_URL` を設定するとPostgreSQL(Neon)に自動で切り替わる。
Render無料プランは再起動でファイルが消えるため、データは外部のNeonに置く構成。

1. [Neon](https://neon.tech) で無料のPostgreSQLを作成し、接続文字列(`postgresql://...`)をコピー
2. このリポジトリをGitHubにpushする
3. [Render](https://render.com) で **New → Blueprint** → このリポジトリを選択(`render.yaml` が自動で読まれる)
4. 環境変数の入力を求められるので設定:
   - `DATABASE_URL`: Neonの接続文字列
   - `LIFF_ID` / `LINE_LOGIN_CHANNEL_ID` / `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET`(LINE Developersからコピー)
   - `DEV_MODE` は設定しない(本番ではOFF)
5. デプロイ完了で `https://〇〇.onrender.com` というURLがもらえる

## LINE Developers 側の設定

| 場所 | 設定する値 |
|---|---|
| LINEログインチャネル > LIFF > エンドポイントURL | `https://〇〇.onrender.com/` |
| Messaging APIチャネル > Webhook URL | `https://〇〇.onrender.com/api/webhook` |

LIFFのURL(`https://liff.line.me/{LIFF_ID}`)を友だちに送れば、LINE内でアプリが開く。

## 注意

- 無料プランのPush通知は月200通まで
- `.env` と `sekipro.db` はGitHubに上げない(.gitignore済み)
