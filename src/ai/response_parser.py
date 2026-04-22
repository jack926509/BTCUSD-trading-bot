class ResponseParser:
    def parse_description(self, raw: str) -> str:
        """
        解析 Claude 回傳的結構描述。
        移除多餘空白，確保不含禁止字詞（在 prompt 層已過濾）。
        """
        if not raw:
            return ""
        lines = [line.strip() for line in raw.strip().splitlines() if line.strip()]
        return " ".join(lines)[:500]
