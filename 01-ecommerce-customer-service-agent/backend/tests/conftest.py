import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".runtime/local.env", override=False)
os.environ["PYTHONUTF8"] = "1"
# 常规回归不得因开发机已配置真实API而产生付费外呼；真实评测使用独立脚本。
os.environ["CS_MODEL_SERVICES_FILE"] = str(ROOT / ".runtime/test-model-services-disabled.json")
