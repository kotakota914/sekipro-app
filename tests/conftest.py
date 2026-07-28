# =============================================================
# pytest の共通設定ファイル
# ここは各テストより先に読み込まれるので、
# main.py を import する前に「テスト用の環境変数」を仕込んでおく
# =============================================================
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))  # リポジトリ直下の main.py を import できるようにする

TEST_DB = pathlib.Path(__file__).parent / "test_sekipro.db"
if TEST_DB.exists():
    TEST_DB.unlink()  # 前回のテストデータが残っていたら消して毎回まっさらにする

os.environ["DEV_MODE"] = "1"          # LINEなしで動く開発モード
os.environ["DB_PATH"] = str(TEST_DB)  # 本物の sekipro.db を汚さない
