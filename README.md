# 課題共有アプリ(セキプロ開発)

LINEの中で動く、グループの課題共有アプリ。
誰か1人が課題をメモすれば、グループ全員のLINEに通知が届く。

- フロントエンド: LIFF + HTML/JS(`static/index.html`)
- バックエンド: Python / FastAPI(`main.py`)
- DB: SQLite(自動で `sekipro.db` が作られる)

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

## 公開して LIFF に貼るURLを作る(Render)

1. このリポジトリをGitHubにpushする
2. [Render](https://render.com) で **New → Web Service** → このリポジトリを選択
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. **Environment** に以下を設定(値はLINE Developersからコピー)
   - `LIFF_ID` / `LINE_LOGIN_CHANNEL_ID` / `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET`
   - `DEV_MODE` は設定しない(本番ではOFF)
4. デプロイ完了で `https://〇〇.onrender.com` というURLがもらえる

## LINE Developers 側の設定

| 場所 | 設定する値 |
|---|---|
| LINEログインチャネル > LIFF > エンドポイントURL | `https://〇〇.onrender.com/` |
| Messaging APIチャネル > Webhook URL | `https://〇〇.onrender.com/api/webhook` |

LIFFのURL(`https://liff.line.me/{LIFF_ID}`)を友だちに送れば、LINE内でアプリが開く。

## 注意

- 無料プランのPush通知は月200通まで
- `.env` と `sekipro.db` はGitHubに上げない(.gitignore済み)
