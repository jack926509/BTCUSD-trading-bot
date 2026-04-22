import os
import yaml
from datetime import datetime


class PromptBuilder:
    def __init__(self):
        config_dir  = os.getenv("CONFIG_DIR", "/app/config")
        config_path = os.path.join(config_dir, "smc_config.yaml")
        with open(config_path, "r") as f:
            self._cfg = yaml.safe_load(f)

        self._forbidden = self._cfg.get("ai", {}).get("forbidden_elements", [])

    def build_description_prompt(self, symbol: str, signal) -> str:
        """
        建立結構描述 prompt。
        Claude 角色：描述者，不負責 BUY/SELL 決策。
        """
        direction     = getattr(signal, "direction", "UNKNOWN")
        source        = getattr(signal, "source", "UNKNOWN")
        htf_bias      = getattr(signal, "htf_bias", "NEUTRAL")
        entry_price   = getattr(signal, "entry_limit_price", None)
        stop_loss     = getattr(signal, "stop_loss", None)
        hard_sl       = getattr(signal, "hard_sl_price", None)
        tp1           = getattr(signal, "take_profit_1", None)
        tp2           = getattr(signal, "take_profit_2", None)
        rrr           = getattr(signal, "rrr", None)
        inval         = getattr(signal, "invalidation_level", None)
        ob_level      = getattr(signal, "ob_level", None)
        fvg_range     = getattr(signal, "fvg_range", None)
        conditions    = getattr(signal, "conditions_met", [])
        price_now     = getattr(signal, "price_at_signal", None)
        displacement  = getattr(signal, "displacement_bars", None)
        swept_level   = getattr(signal, "swept_level", None)

        system_prompt = (
            "你是 BTC/USD SMC 結構描述者，只描述純 Price Action 結構事實，"
            "不提及任何基本面、總經、情緒或新聞。"
            f"禁止出現以下字詞：{', '.join(self._forbidden)}。"
            "以繁體中文輸出，精簡說明（150字以內）。"
        )

        def _p(v) -> str:
            return f"${v:,.0f}" if v else "N/A"

        user_content = f"""
請描述以下 BTC/USD 交易訊號的結構背景（純 Price Action）：

訊號方向：{direction}
訊號來源：{source}
HTF 偏向：{htf_bias}
當前價格：{_p(price_now)}
進場限價：{_p(entry_price)}
止損：{_p(stop_loss)}
Hard SL：{_p(hard_sl)}
目標①：{_p(tp1)}
目標②：{_p(tp2)}
失效條件：{_p(inval)}
RRR：{f'1:{rrr:.2f}' if rrr else 'N/A'}
OB 位置：{_p(ob_level)}
FVG 範圍：{fvg_range if fvg_range else 'N/A'}
位移確認：{f'{displacement} 根' if displacement else 'N/A'}
掠奪位：{_p(swept_level)}
確認條件：{', '.join(conditions) if conditions else 'N/A'}

請以 1-3 句話描述此結構，只說明 Price Action 事實。
""".strip()

        return system_prompt, user_content
