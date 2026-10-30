import re


def error_message(error: Exception) -> str:
    from urllib.error import HTTPError, URLError
    if isinstance(error, PermissionError):
        return "保存先または実行に必要な権限がありません。保存先と対象ゲームの権限を確認してください。"
    if isinstance(error, HTTPError):
        return "配布情報または推論先が要求を受け付けませんでした。接続設定と配布元の状態を確認してください。"
    if isinstance(error, (URLError, ConnectionError, TimeoutError)):
        return "接続を確認できませんでした。既存の経験は保持しています。通信設定またはローカル推論の状態を確認してください。"
    if isinstance(error, (ValueError, OSError)) and re.search('[ぁ-んァ-ン一-龥]', str(error)):
        return prose(str(error))
    return "内部処理に問題が発生しました。技術情報を開発者詳細へ記録しました。"


def prose(text: str) -> str:
    """Keep machine details out of normal views; preserve originals in diagnostics."""
    if re.search(r"```|Traceback|\{[\s\S]*\}|(?:[A-Za-z]:\\|/(?:Users|home|tmp)/)|\b(?:def|class|import)\s|\(module|(?:\w+_){2,}", text):
        return "研究内容の詳細を記録しました。技術情報は開発者詳細で確認できます。"
    return text[:4000]


def system_info() -> dict:
    import os
    import platform
    import shutil
    import ctypes as c
    ram = 0
    if os.name == "nt":
        class Status(c.Structure):
            _fields_ = [("length", c.c_uint32), ("load", c.c_uint32)] + [(name, c.c_uint64) for name in (
                "total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        status = Status(length=c.sizeof(Status))
        if c.windll.kernel32.GlobalMemoryStatusEx(c.byref(status)):
            ram = status.available / 1024**3
    else:
        try:
            ram = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**3
        except (ValueError, OSError):
            pass
    return {"cpu": platform.processor() or platform.machine(), "ram": ram,
            "free": shutil.disk_usage(os.path.expanduser("~")).free / 1024**3}
