"""
集中式 YAML Config Loader  (BE-H3)
──────────────────────────────────
• 所有模組統一從此處取得 config，不再各自載入 YAML
• lru_cache 確保每個 yaml 只讀取一次
• CONFIG_DIR 環境變數統一管理
"""
import os
import yaml
from functools import lru_cache

_CONFIG_DIR = os.getenv("CONFIG_DIR", "/app/config")


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict:
    """載入並快取 YAML config 檔案。檔案不存在時拋出，讓呼叫者決定如何處理。"""
    path = os.path.join(_CONFIG_DIR, name)
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def get_smc_config() -> dict:
    return load_yaml("smc_config.yaml")


def get_risk_config() -> dict:
    return load_yaml("risk_config.yaml")


def get_position_rules() -> dict:
    return load_yaml("position_rules.yaml")


def clear_config_cache():
    """清除所有 YAML config 快取（供測試用）"""
    load_yaml.cache_clear()
